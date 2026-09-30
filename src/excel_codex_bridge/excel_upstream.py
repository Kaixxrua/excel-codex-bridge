"""OpenAI Excel add-in upstream support.

Vendored from Nonary/ghcp_proxy (Unlicense) with minimal changes so it can be
re-synced: the app-path and replay-id imports point at this package, and the
session is never persisted (the bridge re-reads Excel's own cache instead).

The official Excel add-in uses a ChatGPT session.  Keep that credential
isolated in memory and expose only non-secret status information.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from uuid import NAMESPACE_URL, uuid4, uuid5

from . import item_ids as responses_replay_ids


_BASE_MODEL_UPSTREAMS = {
    "gpt-5.6-luna-excel": "gpt-5.6-luna",
    "gpt-5.6-terra-excel": "gpt-5.6-terra",
    "gpt-5.6-sol-excel": "gpt-5.6-sol",
    "gpt-6-sol-excel": "gpt-6-sol",
    "gpt-6-luna-excel": "gpt-6-luna",
    "gpt-6-astra-excel": "gpt-6-astra",
}
LONG_CONTEXT_SUFFIX = "-1m-excel"
# Every model is served twice: a 500k alias, and a ``-1m`` alias of the same
# upstream that runs at the longest input the Excel backend accepts.
EXCEL_MODEL_UPSTREAMS = {
    **_BASE_MODEL_UPSTREAMS,
    **{
        alias.removesuffix("-excel") + LONG_CONTEXT_SUFFIX: upstream
        for alias, upstream in _BASE_MODEL_UPSTREAMS.items()
    },
}
MODEL_IDS = tuple(EXCEL_MODEL_UPSTREAMS)
MODEL_ID = "gpt-5.6-sol-excel"
DEFAULT_CONTEXT_WINDOW = 500_000
# Measured on the real backend for gpt-5.6-sol, gpt-6-sol, gpt-6-luna and
# gpt-6-astra alike: 918,843 input tokens accepted, ~921,375 refused with
# context_length_exceeded. Rounded down.
LONG_CONTEXT_WINDOW = 918_000


def context_window_for(model_id: str) -> int:
    return LONG_CONTEXT_WINDOW if model_id.endswith(LONG_CONTEXT_SUFFIX) else DEFAULT_CONTEXT_WINDOW


def _compaction_limits(context_window: int) -> tuple[int, int]:
    """(Codex auto-compact limit, backend compact_threshold) for a window.

    Codex compacts first; the backend threshold is the fallback for a turn
    that outgrows it, and stays under the window so the backend compacts
    instead of refusing.
    """
    return context_window * 90 // 100 // 1000 * 1000, context_window * 95 // 100 // 1000 * 1000
_UPSTREAM_MODEL_OVERRIDE = os.environ.get("GHCP_EXCEL_UPSTREAM_MODEL", "").strip()
UPSTREAM_MODEL = _UPSTREAM_MODEL_OVERRIDE or EXCEL_MODEL_UPSTREAMS[MODEL_ID]
EXCEL_REASONING_EFFORTS = ("low", "medium", "high", "xhigh")
_REASONING_EFFORT_ALIASES = {
    "x-high": "xhigh",
    "extra-high": "xhigh",
    "extra_high": "xhigh",
    # The backend refuses both (HTTP 422); xhigh is the deepest it has.  Codex
    # itself sends ultra as the catalog's multi_agent_reasoning_effort.
    "max": "xhigh",
    "ultra": "xhigh",
}
EXTERNAL_CLIENT_INSTRUCTIONS = (
    "This request is relayed by an external OpenAI Responses API client, not by "
    "the live Excel workbook. Do not call server-injected Excel, Office, connector, "
    "or workbook tools. Return the answer as assistant text."
)
TOOL_CALL_MARKER_OPEN = "<codex_tool_call>"
TOOL_CALL_MARKER_CLOSE = "</codex_tool_call>"
# Kept only to replay calls produced by proxy versions that used the old text
# marker protocol. New calls travel through Basispoints' declared
# ``run_officejs`` function and are intercepted before any Office code runs.
CLIENT_TOOL_RELAY_PREFIX = "codex_client__"
CLIENT_MARKER_CALL_ID_PREFIX = "call_ghcp_excel_marker_"
NATIVE_FALLBACK_CALL_ID_PREFIX = "call_ghcp_excel_native_"
CLIENT_TOOL_TRANSPORT_NAME = "run_officejs"
CLIENT_TOOL_TRANSPORT_ALIASES = frozenset(
    {CLIENT_TOOL_TRANSPORT_NAME, f"functions.{CLIENT_TOOL_TRANSPORT_NAME}"}
)
# Codex's tool discovery: with it, app and MCP tools stay out of the request
# until a search loads them (the catalog marks the models as supporting it).
TOOL_SEARCH = "tool_search"
TOOL_SEARCH_CALL = "tool_search_call"
TOOL_SEARCH_OUTPUT = "tool_search_output"
# The catalog quotes these namespaces in full; any other (the desktop app's
# tools, an MCP server's) as a one-sentence summary with each parameter's type,
# and a call that misses its parameters gets the full definition back.
FULL_NAMESPACES = frozenset({"collaboration", "image_gen", "web"})
SUMMARY_DESCRIPTION_CHARS = 160
SUMMARY_PARAMETER_CHARS = 60
# tool_search's description quotes what each source (app, MCP server) says of itself.
SEARCH_SOURCE_CHARS = 240
TOOLS_VERSION_METADATA_KEY = "bps_tools_version_id"
_TOOLS_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
_NATIVE_CALL_CACHE_LIMIT = 512
_native_call_cache_lock = threading.Lock()
_native_call_cache: OrderedDict[str, dict] = OrderedDict()
_STORED_CALL_LIMIT = 5000
_STORED_CALL_MAX_AGE = 60 * 86400
_native_call_store_path: str | None = None
_TOOL_CALL_PATTERN = re.compile(
    re.escape(TOOL_CALL_MARKER_OPEN)
    + r"\s*(\{.*?\})\s*"
    + re.escape(TOOL_CALL_MARKER_CLOSE),
    re.DOTALL,
)
_JSON_ESCAPE_CHARS = frozenset('\"\\/bfnrt')
_TRANSPORT_RETRY_GUIDANCE = (
    "The previous run_officejs relay was rejected: {reason}. "
    "Retry once with exactly one outer run_officejs call. Its code field is JSON text, not "
    "JavaScript or OfficeJS, and must contain one catalog-tool object; do not put another "
    "run_officejs wrapper inside it. Serialize the inner JSON before placing it in code, "
    "including any backslashes or quotes in shell commands, and do not repeat the identical payload."
)
_MALFORMED_TRANSPORT = "its transport envelope was malformed"
# Codex takes the message of a collaboration call (spawn_agent, send_message,
# followup_task) for content the backend encrypted unless the call says its
# arguments are plain text, and hands it on to the other agent as such; the
# backend then cannot decrypt it.  Every call the bridge makes has plain arguments.
PLAIN_ARGUMENTS = "encrypted_function_args"
# What the Responses API encrypts (reasoning, agent messages) is a Fernet token.
_SEALED = re.compile(r"gAAAAA[A-Za-z0-9_\-]{40,}={0,2}")
_SEALED_ELSEWHERE = "(an encrypted message from another backend, which cannot be read here)"
RESPONSES_URL = os.environ.get(
    "GHCP_EXCEL_RESPONSES_URL",
    "https://bps.openai.com/basispoints/api/responses",
).strip()
# prompt_cache_key is the documented OpenAI cache-routing control; set to 0
# only if the Basispoints gateway ever starts rejecting the parameter.
FORWARD_PROMPT_CACHE_KEY = os.environ.get(
    "GHCP_EXCEL_FORWARD_PROMPT_CACHE_KEY", "1"
).strip().lower() not in {"0", "false", "no", "off"}
# Escape hatch back to the pre-cache-fix layout (full catalog as the prompt
# suffix) in case the stable-prefix reminder ever stops holding the model to
# the client-tool transport protocol. See _client_tool_protocol_reminder.
CATALOG_AT_PROMPT_END = os.environ.get(
    "GHCP_EXCEL_CATALOG_AT_PROMPT_END", "0"
).strip().lower() in {"1", "true", "yes", "on"}
# The bridge never writes the token anywhere: it re-reads the add-in's own
# WebView cache on every request, so a second encrypted copy is unnecessary.
SESSION_FILE = None

_ALLOWED_CAPTURED_HEADERS = frozenset(
    {
        "authorization",
        "chatgpt-account-id",
        "user-agent",
        "x-basispoints-auth-mode",
        "x-openai-account-id",
        "x-openai-account-user-id",
        "x-openai-internal-basispoints-browser-name",
        "x-openai-internal-basispoints-browser-ua-brands",
        "x-openai-internal-basispoints-browser-ua-mobile",
        "x-openai-internal-basispoints-browser-ua-platform",
        "x-openai-internal-basispoints-client-agent-profile",
        "x-openai-internal-basispoints-client-editor",
        "x-openai-internal-basispoints-client-host",
        "x-openai-internal-basispoints-client-platform",
        "x-openai-internal-basispoints-client-platform-class",
        "x-openai-internal-basispoints-client-product",
        "x-openai-internal-basispoints-client-runtime",
        "x-openai-internal-basispoints-office-host",
        "x-openai-internal-basispoints-office-platform",
        "x-stainless-arch",
        "x-stainless-lang",
        "x-stainless-os",
        "x-stainless-package-version",
        "x-stainless-retry-count",
        "x-stainless-runtime",
        "x-stainless-runtime-version",
    }
)

_DEFAULT_CLIENT_HEADERS = {
    "x-basispoints-auth-mode": "chatgpt",
    "x-openai-internal-basispoints-client-agent-profile": "excel",
    "x-openai-internal-basispoints-client-editor": "excel",
    "x-openai-internal-basispoints-client-host": "office",
    "x-openai-internal-basispoints-client-platform": "excel",
    "x-openai-internal-basispoints-client-platform-class": "PC",
    "x-openai-internal-basispoints-client-product": "basispoints-excel-plugin",
    "x-openai-internal-basispoints-client-runtime": "desktop",
    "x-openai-internal-basispoints-office-host": "Excel",
    "x-openai-internal-basispoints-office-platform": "PC",
    "x-stainless-arch": "unknown",
    "x-stainless-lang": "js",
    "x-stainless-os": "Unknown",
    "x-stainless-package-version": "6.31.0",
    "x-stainless-retry-count": "0",
    "x-stainless-runtime": "browser:chrome",
}

_DISPLAY_NAMES = {
    "gpt-5.6-luna-excel": "5.6-Luna Excel",
    "gpt-5.6-terra-excel": "5.6-Terra Excel",
    "gpt-5.6-sol-excel": "5.6-Sol Excel",
    "gpt-6-sol-excel": "6-Sol Excel",
    "gpt-6-luna-excel": "6-Luna Excel",
    "gpt-6-astra-excel": "6-Astra Excel",
}


def _display_name(model_id: str) -> str:
    base = model_id.replace(LONG_CONTEXT_SUFFIX, "-excel")
    name = _DISPLAY_NAMES.get(base, base.removesuffix("-excel").upper().replace("GPT-", "GPT "))
    return f"{name} 1M" if model_id.endswith(LONG_CONTEXT_SUFFIX) else name


LOCAL_MODEL_CAPABILITIES = {
    model_id: {
        "auto_compact_token_limit": _compaction_limits(context_window_for(model_id))[0],
        "compact_threshold": _compaction_limits(context_window_for(model_id))[1],
        "context_window": context_window_for(model_id),
        "display_name": _display_name(model_id),
        "input_modalities": ["text", "image"],
        "max_context_window": context_window_for(model_id),
        "messages_endpoint_supported": False,
        "model_picker_enabled": True,
        "parallel_tool_calls": True,
        "provider": "OpenAI Excel",
        "reasoning_efforts": list(EXCEL_REASONING_EFFORTS),
        "supported_endpoints": ["/responses"],
        "vision": True,
    }
    for model_id in MODEL_IDS
}


def is_excel_model(model: object) -> bool:
    return excel_model_id(model) is not None


def excel_model_id(model: object) -> str | None:
    if not isinstance(model, str):
        return None
    normalized = model.strip().lower()
    return normalized if normalized in EXCEL_MODEL_UPSTREAMS else None


def _default_compact_threshold(model: object) -> int:
    model_id = excel_model_id(model) or MODEL_ID
    return int(LOCAL_MODEL_CAPABILITIES[model_id]["compact_threshold"])


def upstream_model_for(model: object) -> str:
    model_id = excel_model_id(model) or MODEL_ID
    return _UPSTREAM_MODEL_OVERRIDE or EXCEL_MODEL_UPSTREAMS[model_id]


def _normalize_reasoning_effort(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    normalized = _REASONING_EFFORT_ALIASES.get(normalized, normalized)
    return normalized if normalized in EXCEL_REASONING_EFFORTS else None


def local_model_payload(model_id: str) -> dict[str, object]:
    return {
        "id": model_id,
        "object": "model",
        "created": 0,
        "owned_by": "openai-excel",
    }


def merge_local_model_capabilities(capabilities: dict[str, dict] | None) -> dict[str, dict]:
    merged = dict(capabilities or {})
    merged.update({key: dict(value) for key, value in LOCAL_MODEL_CAPABILITIES.items()})
    return merged


def merge_local_models_payload(payload: dict | None) -> dict:
    result = dict(payload or {})
    raw_data = result.get("data")
    data = [dict(item) for item in raw_data if isinstance(item, dict)] if isinstance(raw_data, list) else []
    data = [item for item in data if item.get("id") != "gpt-excel"]
    existing_ids = {item.get("id") for item in data}
    data.extend(local_model_payload(model_id) for model_id in MODEL_IDS if model_id not in existing_ids)
    result["object"] = result.get("object") or "list"
    result["data"] = data
    return result


def _client_tool_key(name: str, namespace: str | None = None) -> str:
    return f"{namespace}.{name}" if namespace else name


def _iter_client_tools(tools: object, namespace: str | None = None):
    """Yield callable leaves from Codex dynamic tool namespaces."""
    if not isinstance(tools, list):
        return
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        tool_type = str(tool.get("type") or "").strip().lower()
        name = tool.get("name")
        if tool_type == TOOL_SEARCH and namespace is None and tool.get("execution", "client") == "client":
            yield TOOL_SEARCH, TOOL_SEARCH, None, TOOL_SEARCH, tool
            continue
        if tool_type in {"function", "custom"} and isinstance(name, str):
            normalized_name = name.strip()
            if normalized_name:
                yield (
                    _client_tool_key(normalized_name, namespace),
                    normalized_name,
                    namespace,
                    tool_type,
                    tool,
                )
        if tool_type == "namespace" and isinstance(name, str) and name.strip():
            yield from _iter_client_tools(tool.get("tools"), name.strip())


def _loaded_tools(raw_input: object) -> list:
    """The tools Codex's tool_search has loaded so far, from its results in the conversation.

    Codex keeps what it defers (app and MCP tools) out of ``tools`` and hands
    the model only ``tool_search``; the tools a search found come back in its
    result and stay callable from then on, though ``tools`` never lists them.
    """
    if not isinstance(raw_input, list):
        return []
    return [
        tool
        for item in raw_input
        if isinstance(item, dict) and item.get("type") == TOOL_SEARCH_OUTPUT
        for tool in (item.get("tools") if isinstance(item.get("tools"), list) else [])
        if isinstance(tool, dict)
    ]


def _callable_tools(source: dict) -> list:
    declared = source.get("tools") if isinstance(source.get("tools"), list) else []
    return [*declared, *_loaded_tools(source.get("input"))]


def declared_tool_types(source: dict) -> dict[str, str]:
    """The tools the request declares: what the catalog at the top of the prompt lists."""
    if str(source.get("tool_choice") or "").strip().lower() == "none":
        return {}
    return {key: tool_type for key, _name, _namespace, tool_type, _tool in _iter_client_tools(source.get("tools"))}


def client_tool_types(source: dict) -> dict[str, str]:
    """Every tool the model may call: those declared, and those tool_search loaded."""
    if str(source.get("tool_choice") or "").strip().lower() == "none":
        return {}
    result: dict[str, str] = {}
    for key, _name, _namespace, tool_type, _tool in _iter_client_tools(_callable_tools(source)):
        result[key] = tool_type
    return result


# Responses Lite: the input item that carries the tools.
LITE_TOOLS_ITEM = "additional_tools"
# Codex's default tool namespace: a tool in it is called by its bare name.
DEFAULT_TOOL_NAMESPACE = "functions"


def _tool_identity(tool: dict) -> tuple[str, object]:
    return ("namespace" if tool.get("type") == "namespace" else "tool", tool.get("name"))


def from_responses_lite(body: dict) -> dict:
    """``body`` in the usual Responses shape if Codex sent it the Responses Lite way; else ``body``.

    Codex's own entries for gpt-5.6 and gpt-6 models, which it uses without
    the bridge's model catalog (through a relay's config template, say), send
    no ``tools`` and no ``instructions``.  The tools come in an
    ``additional_tools`` input item, the plain ones in the ``functions``
    namespace, and the instructions as the developer message right after it.
    The tool catalog is built from ``tools``: left as they came, the model
    would see no tools, and every call it made would name a tool not there.
    """
    items = body.get("input")
    if not isinstance(items, list) or not any(
        isinstance(item, dict) and item.get("type") == LITE_TOOLS_ITEM for item in items
    ):
        return body
    tools = [tool for tool in body.get("tools") or [] if isinstance(tool, dict)]
    known = {_tool_identity(tool) for tool in tools}
    kept: list = []
    instructions = body.get("instructions")
    lift_instructions = not (isinstance(instructions, str) and instructions.strip())
    after_tools = False
    for item in items:
        if not isinstance(item, dict) or item.get("type") != LITE_TOOLS_ITEM:
            text = _lite_instructions(item) if after_tools and lift_instructions else None
            if text is not None:
                instructions, lift_instructions = text, False
            else:
                kept.append(item)
            after_tools = False
            continue
        after_tools = True
        for tool in item.get("tools") or []:
            if not isinstance(tool, dict):
                continue
            plain = tool.get("type") == "namespace" and tool.get("name") == DEFAULT_TOOL_NAMESPACE
            for lifted in (tool.get("tools") or []) if plain else [tool]:
                if isinstance(lifted, dict) and _tool_identity(lifted) not in known:
                    known.add(_tool_identity(lifted))
                    tools.append(lifted)
    normalized = {**body, "input": kept, "tools": tools}
    if isinstance(instructions, str) and instructions.strip():
        normalized["instructions"] = instructions
    return normalized


def _lite_instructions(item: object) -> str | None:
    """The text of a Responses Lite instructions message: one developer message of text alone."""
    if not isinstance(item, dict) or item.get("type", "message") != "message" or item.get("role") != "developer":
        return None
    content = item.get("content")
    if isinstance(content, str):
        return content if content.strip() else None
    if not isinstance(content, list) or len(content) != 1:
        return None
    part = content[0]
    if not isinstance(part, dict) or part.get("type") != "input_text" or not isinstance(part.get("text"), str):
        return None
    return part["text"] if part["text"].strip() else None


def relay_tool_name(name: str) -> str:
    """Return the legacy non-colliding marker name used before run_officejs."""
    return CLIENT_TOOL_RELAY_PREFIX + name


def _original_client_tool_name(
    name: object,
    allowed_tools: dict[str, str],
) -> str | None:
    if not isinstance(name, str):
        return None
    if name.startswith(CLIENT_TOOL_RELAY_PREFIX):
        candidate = name[len(CLIENT_TOOL_RELAY_PREFIX) :]
        return candidate if candidate in allowed_tools else None
    # Accept the old, unprefixed marker format for in-flight responses. Native
    # Basispoints calls also arrive unprefixed; schema validation below decides
    # whether one can safely stand in for a same-named client tool.
    if name in allowed_tools:
        return name
    # functions.exec is exec: Codex's default namespace, as a host may display it.
    plain = name.removeprefix(f"{DEFAULT_TOOL_NAMESPACE}.")
    return plain if plain != name and plain in allowed_tools else None


# Codex's code mode gives the model only these: exec runs JavaScript that calls the other tools.
CODE_MODE_TOOLS = ("exec", "wait")
CODE_MODE_EXAMPLE = json.dumps(
    {"name": "exec", "input": 'const result = await tools.exec_command({cmd: "pwd"});\ntext(result.output);'},
    separators=(",", ":"),
)


def _code_mode(allowed_tools: dict[str, str]) -> bool:
    """Whether Codex runs this catalog in code mode (its exec is a custom tool; exec_command is a function)."""
    return allowed_tools.get(CODE_MODE_TOOLS[0]) == "custom"


def keep_native_calls_in(path: str | os.PathLike | None) -> None:
    """Also keep remembered native calls in a SQLite file, so they survive restarts.

    Codex drops the upstream item id and the model's own summary/references
    from the tool calls it sends back; only the original item replays exactly.
    Without this file a restarted bridge (a reopened desktop window, or
    ``codex resume``) could only rebuild approximate items for earlier calls.
    """
    global _native_call_store_path
    if path is None:
        _native_call_store_path = None
        return
    try:
        with contextlib.closing(sqlite3.connect(os.fspath(path), timeout=5)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS native_calls "
                "(call_id TEXT PRIMARY KEY, item TEXT NOT NULL, saved_at REAL NOT NULL)"
            )
            db.execute("DELETE FROM native_calls WHERE saved_at < ?", (time.time() - _STORED_CALL_MAX_AGE,))
            db.execute(
                "DELETE FROM native_calls WHERE call_id NOT IN "
                "(SELECT call_id FROM native_calls ORDER BY saved_at DESC LIMIT ?)",
                (_STORED_CALL_LIMIT,),
            )
    except (sqlite3.Error, OSError):
        _native_call_store_path = None
        return
    # Tool arguments can hold workbook contents.
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    _native_call_store_path = os.fspath(path)


def _store_native_call(call_id: str, item: dict) -> None:
    path = _native_call_store_path
    if path is None:
        return
    try:
        with contextlib.closing(sqlite3.connect(path, timeout=5)) as db, db:
            db.execute(
                "INSERT OR REPLACE INTO native_calls (call_id, item, saved_at) VALUES (?, ?, ?)",
                (call_id, json.dumps(item, ensure_ascii=False), time.time()),
            )
    except (sqlite3.Error, OSError):
        pass


def _stored_native_call(call_id: str) -> dict | None:
    path = _native_call_store_path
    if path is None:
        return None
    try:
        with contextlib.closing(sqlite3.connect(path, timeout=5)) as db:
            row = db.execute("SELECT item FROM native_calls WHERE call_id = ?", (call_id,)).fetchone()
        item = json.loads(row[0]) if row else None
    except (sqlite3.Error, OSError, ValueError):
        return None
    return item if isinstance(item, dict) else None


def _remember_native_call(item: dict) -> None:
    call_id = item.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        return
    with _native_call_cache_lock:
        _native_call_cache[call_id] = copy.deepcopy(item)
        _native_call_cache.move_to_end(call_id)
        while len(_native_call_cache) > _NATIVE_CALL_CACHE_LIMIT:
            _native_call_cache.popitem(last=False)
    _store_native_call(call_id, item)


def _remembered_native_call(call_id: object) -> dict | None:
    if not isinstance(call_id, str) or not call_id:
        return None
    with _native_call_cache_lock:
        item = _native_call_cache.get(call_id)
        if item is not None:
            _native_call_cache.move_to_end(call_id)
            return copy.deepcopy(item)
    item = _stored_native_call(call_id)
    if item is None:
        return None
    with _native_call_cache_lock:
        _native_call_cache[call_id] = copy.deepcopy(item)
        while len(_native_call_cache) > _NATIVE_CALL_CACHE_LIMIT:
            _native_call_cache.popitem(last=False)
    return item


def _client_tool_specs(source: dict) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for key, name, namespace, tool_type, tool in _iter_client_tools(_callable_tools(source)):
        result[key] = {
            "key": key,
            "name": name,
            "namespace": namespace,
            "type": tool_type,
            "spec": tool,
        }
    return result


def _decode_transport_code(code: object) -> dict | None:
    if isinstance(code, dict):
        return code
    if not isinstance(code, str):
        return None

    candidates = [code]
    repaired = _repair_invalid_json_backslashes(code)
    if repaired != code:
        candidates.append(repaired)

    # Models occasionally wrap the requested JSON in a code fence or a
    # one-line assignment despite the exact-format instruction. Decode the
    # first complete JSON object without ever evaluating the surrounding
    # text as JavaScript. The repair pass only doubles backslashes that are
    # invalid JSON escapes inside string values (for example ``\\(`` in a
    # shell regex), preserving the command rather than executing anything.
    decoder = json.JSONDecoder()
    for candidate_text in candidates:
        try:
            envelope = json.loads(candidate_text)
        except json.JSONDecodeError:
            envelope = None
            for index, character in enumerate(candidate_text):
                if character != "{":
                    continue
                try:
                    candidate, _ = decoder.raw_decode(candidate_text[index:])
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict):
                    envelope = candidate
                    break
        if isinstance(envelope, dict):
            return envelope
    return None


def _repair_invalid_json_backslashes(text: str) -> str:
    """Double invalid backslashes inside JSON string values."""
    repaired: list[str] = []
    in_string = False
    index = 0
    while index < len(text):
        character = text[index]
        if not in_string:
            repaired.append(character)
            if character == '\"':
                in_string = True
            index += 1
            continue
        if character == '\"':
            repaired.append(character)
            in_string = False
            index += 1
            continue
        if character != '\\':
            repaired.append(character)
            index += 1
            continue

        next_character = text[index + 1] if index + 1 < len(text) else ""
        valid_escape = next_character in _JSON_ESCAPE_CHARS
        if next_character == "u":
            valid_escape = (
                index + 5 < len(text)
                and all(
                    digit in "0123456789abcdefABCDEF"
                    for digit in text[index + 2 : index + 6]
                )
            )
        if valid_escape:
            repaired.extend((character, next_character))
            index += 2
        else:
            repaired.extend((character, character))
            index += 1
    return "".join(repaired)


def _is_transport_name(name: object) -> bool:
    return isinstance(name, str) and name in CLIENT_TOOL_TRANSPORT_ALIASES


def _transport_envelope(native: dict) -> dict | None:
    if (
        native.get("type") != "function_call"
        or not _is_transport_name(native.get("name"))
    ):
        return None
    raw_arguments = native.get("arguments")
    if not isinstance(raw_arguments, str):
        return None
    try:
        arguments = json.loads(raw_arguments)
    except json.JSONDecodeError:
        return None
    if not isinstance(arguments, dict):
        return None
    envelope = _decode_transport_code(arguments.get("code"))
    for _ in range(2):
        if envelope is None or not _is_transport_name(envelope.get("name")):
            break
        nested_arguments = envelope.get("arguments")
        if isinstance(nested_arguments, str):
            try:
                nested_arguments = json.loads(nested_arguments)
            except json.JSONDecodeError:
                return None
        if not isinstance(nested_arguments, dict):
            return None
        envelope = _decode_transport_code(nested_arguments.get("code"))
    if envelope is not None and _is_transport_name(envelope.get("name")):
        return None
    return envelope


def _value_matches_schema(value: object, schema: object) -> bool:
    if not isinstance(schema, dict) or not schema:
        return True
    expected_type = schema.get("type")
    if isinstance(expected_type, list):
        return any(
            _value_matches_schema(value, {**schema, "type": candidate})
            for candidate in expected_type
        )
    if expected_type == "object":
        if not isinstance(value, dict):
            return False
        required = schema.get("required")
        if isinstance(required, list) and any(
            isinstance(key, str) and key not in value for key in required
        ):
            return False
        properties = schema.get("properties")
        if isinstance(properties, dict):
            if schema.get("additionalProperties") is False and any(
                key not in properties for key in value
            ):
                return False
            for key, nested_value in value.items():
                nested_schema = properties.get(key)
                if nested_schema is not None and not _value_matches_schema(
                    nested_value,
                    nested_schema,
                ):
                    return False
    elif expected_type == "array":
        if not isinstance(value, list):
            return False
        item_schema = schema.get("items")
        if item_schema is not None and any(
            not _value_matches_schema(item, item_schema) for item in value
        ):
            return False
    elif expected_type == "string" and not isinstance(value, str):
        return False
    elif expected_type == "integer" and (
        not isinstance(value, int) or isinstance(value, bool)
    ):
        return False
    elif expected_type == "number" and (
        not isinstance(value, (int, float)) or isinstance(value, bool)
    ):
        return False
    elif expected_type == "boolean" and not isinstance(value, bool):
        return False
    elif expected_type == "null" and value is not None:
        return False
    enum = schema.get("enum")
    return not isinstance(enum, list) or value in enum


_PLAN_STATUS_BY_ALIAS = {
    "pending": "pending",
    "not_started": "pending",
    "todo": "pending",
    "planned": "pending",
    "queued": "pending",
    "blocked": "pending",
    "in_progress": "in_progress",
    "active": "in_progress",
    "started": "in_progress",
    "doing": "in_progress",
    "current": "in_progress",
    "completed": "completed",
    "complete": "completed",
    "done": "completed",
    "finished": "completed",
}


def _normalize_plan_status(status: object) -> str | None:
    if not isinstance(status, str):
        return None
    key = status.strip().lower().replace("-", "_").replace(" ", "_")
    return _PLAN_STATUS_BY_ALIAS.get(key, status)


def _normalize_native_function_arguments(name: str, arguments: dict) -> dict:
    if name != "update_plan":
        return arguments
    plan = arguments.get("plan")
    if not isinstance(plan, list):
        return arguments
    normalized_plan = []
    for item in plan:
        if not isinstance(item, dict):
            continue
        step = item.get("step")
        if not isinstance(step, str):
            step = item.get("description")
        if not isinstance(step, str):
            step = item.get("title")
        status = _normalize_plan_status(item.get("status"))
        if isinstance(step, str) and isinstance(status, str):
            normalized_plan.append({"step": step, "status": status})
    normalized: dict[str, object] = {"plan": normalized_plan}
    explanation = arguments.get("explanation")
    if not isinstance(explanation, str):
        explanation = arguments.get("summary")
    if isinstance(explanation, str) and explanation:
        normalized["explanation"] = explanation
    return normalized


def _restore_native_function_arguments(name: str, arguments: object) -> object:
    """Restore the Basispoints schema after a native call visits Codex."""
    if name != "update_plan":
        return arguments
    parsed = arguments
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except json.JSONDecodeError:
            return arguments
    if not isinstance(parsed, dict) or not isinstance(parsed.get("plan"), list):
        return arguments

    native_plan: list[dict[str, str]] = []
    for index, item in enumerate(parsed["plan"]):
        if not isinstance(item, dict):
            continue
        step = item.get("step")
        status = item.get("status")
        if not isinstance(step, str) or not isinstance(status, str):
            continue
        native_plan.append(
            {
                "id": f"step{index + 1}",
                "description": step,
                "status": status,
                "result": "",
            }
        )
    explanation = parsed.get("explanation")
    native = {
        "summary": (
            explanation
            if isinstance(explanation, str) and explanation
            else "Update task plan"
        ),
        "plan": native_plan,
    }
    return json.dumps(native, separators=(",", ":"), ensure_ascii=False)


def parallel_tool_calls_allowed(source: dict) -> bool:
    """Codex asks for parallel tool calls; the Responses API default is to allow them."""
    return source.get("parallel_tool_calls") is not False


def extract_native_client_tool_calls(
    response: dict | None,
    source: dict,
) -> list[dict[str, str]]:
    """The client tool calls in a response, in order; unusable native calls are left out.

    The model issues independent calls together, each through its own
    run_officejs call. A client that turned parallel calls off gets only the
    first; the model sees its result and asks again for anything it still needs.
    """
    if not isinstance(response, dict):
        return []
    output = response.get("output")
    if not isinstance(output, list):
        return []
    specs = _client_tool_specs(source)
    allowed_tools = client_tool_types(source)
    calls: list[dict[str, str]] = []
    for native in output:
        if not isinstance(native, dict) or native.get("type") not in {"function_call", "custom_tool_call"}:
            continue
        call = _client_call_from_native(native, specs, allowed_tools)
        if call is None:
            continue
        calls.append(call)
        if not parallel_tool_calls_allowed(source):
            break
    return calls


def extract_native_client_tool_call(
    response: dict | None,
    source: dict,
) -> dict[str, str] | None:
    calls = extract_native_client_tool_calls(response, source)
    return calls[0] if calls else None


def _tool_search_arguments(arguments: dict) -> dict | None:
    """tool_search's arguments as Codex reads them: a query, and a whole positive limit if any."""
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        return None
    search: dict[str, object] = {"query": query}
    limit = arguments.get("limit")
    if isinstance(limit, (int, float)) and not isinstance(limit, bool) and limit >= 1 and limit == int(limit):
        search["limit"] = int(limit)
    return search


def _tool_search_call(call_id: str, search: dict) -> dict:
    return {
        "type": TOOL_SEARCH_CALL,
        "id": f"tsc_{call_id}",
        "call_id": call_id,
        "execution": "client",
        "arguments": search,
    }


def _client_call_from_native(
    native: dict,
    specs: dict[str, dict],
    allowed_tools: dict[str, str],
) -> dict[str, str] | None:
    envelope = _transport_envelope(native)
    if _is_transport_name(native.get("name")) and envelope is None:
        return None
    name = (
        _original_client_tool_name(envelope.get("name"), allowed_tools)
        if envelope is not None
        else _original_client_tool_name(native.get("name"), allowed_tools)
    )
    if name is None or name not in specs:
        return None
    tool_info = specs[name]
    spec = tool_info["spec"]
    expected_type = tool_info["type"]
    if expected_type in {"function", TOOL_SEARCH}:
        if envelope is not None:
            arguments = envelope.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    return None
        else:
            if native.get("type") != "function_call":
                return None
            raw_arguments = native.get("arguments")
            if not isinstance(raw_arguments, str):
                return None
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError:
                return None
        if not isinstance(arguments, dict):
            return None
        if envelope is None:
            arguments = _normalize_native_function_arguments(name, arguments)
        input_schema = (
            spec.get("parameters")
            or spec.get("inputSchema")
            or spec.get("input_schema")
        )
        if not _value_matches_schema(arguments, input_schema):
            return None
        native_call_id = native.get("call_id")
        call_id = (
            native_call_id
            if isinstance(native_call_id, str) and native_call_id
            else f"{NATIVE_FALLBACK_CALL_ID_PREFIX}{uuid4().hex}"
        )
        native_item_id = native.get("id")
        if expected_type == TOOL_SEARCH:
            search = _tool_search_arguments(arguments)
            if search is None:
                return None
            _remember_native_call(native)
            return _tool_search_call(call_id, search)
        _remember_native_call(native)
        result = {
            "type": "function_call",
            "id": (
                native_item_id
                if isinstance(native_item_id, str) and native_item_id
                else responses_replay_ids.function_item_id(call_id)
            ),
            "call_id": call_id,
            "name": tool_info["name"],
            "arguments": json.dumps(
                arguments,
                separators=(",", ":"),
                ensure_ascii=False,
            ),
            PLAIN_ARGUMENTS: [],
        }
        if tool_info["namespace"]:
            result["namespace"] = tool_info["namespace"]
        return result
    if expected_type == "custom":
        custom_input = (
            envelope.get("input")
            if envelope is not None
            else native.get("input")
        )
        if envelope is None and native.get("type") != "custom_tool_call":
            return None
        if not isinstance(custom_input, str):
            return None
        native_call_id = native.get("call_id")
        call_id = (
            native_call_id
            if isinstance(native_call_id, str) and native_call_id
            else f"{NATIVE_FALLBACK_CALL_ID_PREFIX}{uuid4().hex}"
        )
        native_item_id = native.get("id")
        _remember_native_call(native)
        return {
            "type": "custom_tool_call",
            "id": (
                native_item_id
                if (
                    envelope is None
                    and isinstance(native_item_id, str)
                    and native_item_id
                )
                else f"ctc_{call_id}"
            ),
            "call_id": call_id,
            "name": name,
            "input": custom_input,
        }
    return None


def _parameters(tool: dict) -> dict:
    parameters = tool.get("parameters") or tool.get("inputSchema") or tool.get("input_schema")
    return parameters if isinstance(parameters, dict) else {}


def _compact_schema(schema: object) -> object:
    """``schema`` without what only tells a validator something (the bridge checks calls against the original)."""
    if isinstance(schema, list):
        return [_compact_schema(value) for value in schema]
    if not isinstance(schema, dict):
        return schema
    compact: dict[str, object] = {}
    for key, value in schema.items():
        if key in {"title", "$schema"} or (key == "additionalProperties" and value is False):
            continue
        if key in {"properties", "patternProperties", "$defs", "definitions"} and isinstance(value, dict):
            # Names, not keywords: a parameter may well be called title.
            compact[key] = {name: _compact_schema(nested) for name, nested in value.items()}
        elif key in {"enum", "const", "default", "examples"}:
            compact[key] = value
        else:
            compact[key] = _compact_schema(value)
    return compact


_SENTENCE_END = re.compile(r"[.!?](?=\s+[^a-z\s]|\s*$)|[\u3002\uff01\uff1f]")


def _leading_sentences(text: object, limit: int) -> str:
    """The whole sentences ``text`` starts with that fit in ``limit`` characters; at least the first, cut to fit."""
    text = " ".join(text.split()) if isinstance(text, str) else ""
    if len(text) <= limit:
        return text
    kept = ""
    for match in _SENTENCE_END.finditer(text):
        if match.end() > limit:
            break
        kept = text[: match.end()]
    return kept or text[: limit - 3].rstrip() + "..."


def _type_summary(schema: object) -> str:
    """A short type for ``schema``: string, number[], "a"|"b", {name: string; size?: number}."""
    if not isinstance(schema, dict):
        return "any"
    if isinstance(schema.get("enum"), list):
        return "|".join(json.dumps(value, ensure_ascii=False) for value in schema["enum"])
    if "const" in schema:
        return json.dumps(schema["const"], ensure_ascii=False)
    variants = schema.get("anyOf") or schema.get("oneOf")
    if isinstance(variants, list) and variants:
        return "|".join(_type_summary(variant) for variant in variants)
    kind = schema.get("type")
    if kind == "array":
        return _type_summary(schema.get("items")) + "[]"
    if kind == "object" and isinstance(schema.get("properties"), dict):
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        return "{" + "; ".join(
            f"{name}{'' if name in required else '?'}: {_type_summary(nested)}"
            for name, nested in schema["properties"].items()
        ) + "}"
    if isinstance(kind, list):
        return "|".join(str(item) for item in kind)
    return str(kind) if kind else "any"


def _parameter_summary(schema: dict) -> dict[str, str]:
    """Each parameter as "type, required: what it is" (the start of its description)."""
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return {}
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    summary = {}
    for name, nested in properties.items():
        text = _type_summary(nested) + (", required" if name in required else "")
        described = _leading_sentences(nested.get("description") if isinstance(nested, dict) else None,
                                    SUMMARY_PARAMETER_CHARS)
        summary[name] = f"{text}: {described}" if described else text
    return summary


def _search_description(description: str) -> str:
    """tool_search's description with the start of what it says for each source (it quotes each in full)."""
    return "\n".join(
        f"- {_leading_sentences(line[2:], SEARCH_SOURCE_CHARS)}" if line.startswith("- ") else line
        for line in description.splitlines()
    )


def _summarized(namespace: str | None) -> bool:
    return namespace is not None and namespace not in FULL_NAMESPACES


def _catalog_entry(key: str, namespace: str | None, tool_type: str, tool: dict, *, full: bool = False) -> dict:
    """How the catalog shows one tool; a summary for a namespace of the desktop app's or an MCP server's."""
    entry: dict[str, object] = {"type": "function" if tool_type == TOOL_SEARCH else tool_type, "name": key}
    description = tool.get("description")
    if not full and _summarized(namespace) and tool_type == "function":
        entry["summary"] = _leading_sentences(description, SUMMARY_DESCRIPTION_CHARS)
        entry["parameters"] = _parameter_summary(_parameters(tool))
        return entry
    if isinstance(description, str) and description:
        entry["description"] = _search_description(description) if tool_type == TOOL_SEARCH else description
    if tool_type in {"function", TOOL_SEARCH}:
        entry["parameters"] = _compact_schema(_parameters(tool))
    elif isinstance(tool.get("format"), dict):
        entry["format"] = tool["format"]
    return entry


def _catalog_json(tools: object, *, full: bool = False) -> str:
    return json.dumps(
        [
            _catalog_entry(key, namespace, tool_type, tool, full=full)
            for key, _name, namespace, tool_type, tool in _iter_client_tools(tools)
        ],
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _catalog_notes(source: dict) -> str:
    """How to read the summarized entries and tool_search's results, when the catalog has them."""
    tools = list(_iter_client_tools(source.get("tools")))
    notes = ""
    if any(_summarized(namespace) and tool_type == "function" for _key, _name, namespace, tool_type, _tool in tools):
        notes += (
            "An entry with summary instead of description gives each parameter as "
            '"type[, required]: meaning" (a trailing ? marks an optional field); a call that does not '
            "match the tool comes back with its full definition. "
        )
    if any(tool_type == TOOL_SEARCH for _key, _name, _namespace, tool_type, _tool in tools):
        notes += (
            "tool_search loads more tools (such as app and MCP tools) that this catalog leaves out; "
            "its result defines the tools it loaded, and you call them through run_officejs like "
            "catalog tools, by the full name it gives. "
        )
    return notes


def _client_tool_protocol_instructions(source: dict) -> str:
    allowed_tools = declared_tool_types(source)
    if not allowed_tools:
        return EXTERNAL_CLIENT_INSTRUCTIONS

    catalog_json = _catalog_json(source.get("tools"))
    if parallel_tool_calls_allowed(source):
        closing = (
            "\nRemember: each outer native run_officejs call carries exactly one "
            "catalog-tool JSON object in its code field. When several tool calls do not "
            "depend on each other (for example reading several files, or independent "
            "commands), make them as separate run_officejs calls in the same response; "
            "wait for a result only when the next call needs it. "
        )
    else:
        closing = (
            "\nRemember: call the outer native run_officejs tool once; put exactly one "
            "catalog-tool JSON object in its code field. "
        )
    if _code_mode(allowed_tools):
        inspection = (
            "Codex runs this catalog in code mode: exec is the tool for work, and its input is "
            "JavaScript that calls the other tools (shell commands through exec_command, and so on) "
            "on the global tools object, as exec's description sets out. For repository inspection, "
            "run shell commands through exec. "
        )
        function_example = '{"name":"wait","arguments":{"cell_id":"1"}}'
        example = f"For example, to run pwd, code is {CODE_MODE_EXAMPLE}. "
        no_javascript = (
            "Do not put OfficeJS, a second run_officejs envelope, or a functions.run_officejs "
            "wrapper inside code. The field is named code for compatibility; it is not JavaScript: "
            "JavaScript goes only in exec's input, as a string in that JSON object. "
        )
    else:
        inspection = (
            "For repository inspection, invoke a suitable catalog shell tool (for example exec_command) "
            "through run_officejs. "
        )
        function_example = '{"name":"exec_command","arguments":{"cmd":"pwd"}}'
        example = ""
        no_javascript = (
            "Do not put JavaScript, OfficeJS, a second run_officejs envelope, or a "
            "functions.run_officejs wrapper inside code. The field is named code for compatibility; it is "
            "not JavaScript. "
        )
    return (
        "This request is relayed by an external Codex Responses API client, not "
        "by the live Excel workbook. This proxy instruction supersedes any earlier "
        "description of run_officejs as an OfficeJS executor. The native run_officejs function is a "
        "transport endpoint owned by this proxy for this request. The proxy "
        "intercepts it before execution, so it never runs Office code or changes "
        "the workbook. Every client tool in the JSON catalog is available through "
        "that transport. Other native server-injected Excel, Office, connector, "
        "workbook, list_skills, and web-search tools are unavailable. "
        "Never claim shell, filesystem, or workspace access is unavailable when the "
        "catalog contains a suitable tool. "
        + inspection
        + "Transport has two layers and they must not be mixed: the outer native "
        "tool is run_officejs (some hosts display it as functions.run_officejs); "
        "the inner code value is JSON text containing exactly one compact JSON object for one catalog "
        "client tool. The inner name is never run_officejs or functions.run_officejs. "
        "For a function tool, use this shape: outer arguments include summary, "
        "extended_summary, destructive=false, references=[], and code equal to "
        + function_example
        + ". For a custom tool, code instead contains "
        '{"name":"TOOL_NAME","input":"RAW_INPUT"}. '
        + example
        + no_javascript
        + "Serialize the complete inner object before placing it there, especially when "
        "shell commands contain backslashes or quotes. TOOL_NAME and its payload must follow the "
        "catalog exactly, with only the parameters the tool defines. "
        + _catalog_notes(source)
        + "The proxy converts this native function call into the "
        "real client tool call, then replays the original run_officejs identity "
        "with the client tool result on the next request. Interpret that result as "
        "the named client tool's output. Native update_plan may be used normally "
        "when update_plan is in the catalog, but after it succeeds take the next "
        "substantive action through run_officejs. Do not stop at commentary saying "
        "you will take an action: make the tool call in the same response. Never "
        "repeat a tool request whose output is already present. Available client "
        "tools:\n"
        + catalog_json
        + closing
        + "A host prefix such as "
        "functions. is only display syntax, not an inner client-tool name."
    )


def _client_tool_protocol_reminder(source: dict) -> str:
    """Compact protocol cue that stays inside the cached prompt prefix.

    The catalog itself is ~3.5k tokens.  While it sat at the end of the prompt
    it re-billed as fresh input on *every* turn: the upstream prompt cache can
    only extend to the point where the previous request diverged, and appending
    new history in front of a trailing catalog puts that divergence right at
    the catalog's first byte.  Wire captures showed a hard floor of ~3.8k fresh
    input tokens per request for exactly that reason.  So the catalog moved
    into the cached prefix. This reminder must stay there too: appending it
    after conversation history would make the next request insert new items
    before the previous request's final item and break the strict extension
    needed for the upstream cache to reuse the growing conversation.
    """
    allowed_tools = declared_tool_types(source)
    if not allowed_tools:
        return ""
    code_mode = _code_mode(allowed_tools)
    reminder = (
        "Reminder: use the outer native run_officejs transport (a host may display "
        "it as functions.run_officejs); it never executes Office code here. Put "
        "exactly one JSON object as JSON text in code, with name set to one client tool "
        "of the catalog above. Never set the inner name to run_officejs or functions.run_officejs, "
        "and never nest another transport envelope. The code field is not JavaScript"
        + (" (JavaScript goes only in exec's input)" if code_mode else "")
        + "; serialize the inner JSON and escape backslashes and quotes in shell commands. Example inner code: "
        + (CODE_MODE_EXAMPLE if code_mode else '{"name":"exec_command","arguments":{"cmd":"pwd"}}')
        + ". Do not merely say you will act or that access is unavailable. "
        "Other native tools are unavailable."
    )
    if "shell_command" in allowed_tools:
        reminder += " For repository inspection transport shell_command."
    elif "exec_command" in allowed_tools:
        reminder += " For repository inspection transport exec_command."
    elif code_mode:
        reminder += (
            " For repository inspection transport exec, with JavaScript that calls tools.exec_command;"
            " the other tools are called from exec the same way."
        )
    custom_tools = sorted(
        name for name, tool_type in allowed_tools.items() if tool_type == "custom"
    )
    if custom_tools:
        reminder += (
            " Custom tools use input, not arguments: "
            '{"name":"TOOL_NAME","input":"RAW_INPUT"}. '
        )
    if allowed_tools.get("apply_patch") == "custom":
        reminder += (
            "For apply_patch, put the complete raw patch in input; "
            "never use arguments.patch."
        )
    if "update_plan" in allowed_tools:
        reminder += (
            " Native update_plan is allowed for progress; after its result, "
            "take the next substantive action through run_officejs."
        )
    return reminder


def extract_client_tool_call(
    text: str,
    allowed_tools: dict[str, str],
) -> dict[str, str] | None:
    if not isinstance(text, str) or not allowed_tools:
        return None
    match = _TOOL_CALL_PATTERN.search(text)
    if match is None:
        return None
    try:
        marker = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(marker, dict):
        return None
    marker_name = marker.get("name")
    if not isinstance(marker_name, str):
        return None
    name = _original_client_tool_name(marker_name.strip(), allowed_tools)
    if name is None:
        return None
    tool_type = allowed_tools.get(name)
    if tool_type in {"function", TOOL_SEARCH}:
        arguments = marker.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                return None
        if not isinstance(arguments, dict):
            return None
        call_id = f"{CLIENT_MARKER_CALL_ID_PREFIX}{uuid4().hex}"
        if tool_type == TOOL_SEARCH:
            search = _tool_search_arguments(arguments)
            return _tool_search_call(call_id, search) if search is not None else None
        return {
            "type": "function_call",
            "id": responses_replay_ids.function_item_id(call_id),
            "call_id": call_id,
            "name": name,
            "arguments": json.dumps(
                arguments,
                separators=(",", ":"),
                ensure_ascii=False,
            ),
            PLAIN_ARGUMENTS: [],
        }
    if tool_type == "custom":
        custom_input = marker.get("input")
        if not isinstance(custom_input, str):
            return None
        call_id = f"{CLIENT_MARKER_CALL_ID_PREFIX}{uuid4().hex}"
        return {
            "type": "custom_tool_call",
            "id": f"ctc_{call_id}",
            "call_id": call_id,
            "name": name,
            "input": custom_input,
        }
    return None


def response_payload_with_tool_call(
    response: dict | None,
    tool_call: dict[str, str],
    *,
    model_id: str = MODEL_ID,
) -> dict[str, object]:
    return response_payload_with_tool_calls(response, [tool_call], model_id=model_id)


def response_payload_with_tool_calls(
    response: dict | None,
    tool_calls: list[dict[str, str]],
    *,
    model_id: str = MODEL_ID,
) -> dict[str, object]:
    result = dict(response or {})
    result.setdefault("id", f"resp_{uuid4().hex}")
    result.setdefault("object", "response")
    result.setdefault("created_at", int(time.time()))
    result["status"] = "completed"
    result["model"] = model_id
    completed = [{**tool_call, "status": "completed"} for tool_call in tool_calls]
    pending = list(completed)
    existing_output = result.get("output")
    replaced_native_call = False
    output: list[dict] = []
    if isinstance(existing_output, list):
        for item in existing_output:
            if isinstance(item, dict) and item.get("type") in {"function_call", "custom_tool_call"}:
                # The converted calls take the native calls' places in order; a
                # native call that could not be converted is dropped.
                replaced_native_call = True
                if pending:
                    output.append(pending.pop(0))
            elif isinstance(item, dict):
                output.append(item)
    output.extend(pending)
    result["output"] = output if replaced_native_call else completed
    result["error"] = None
    result["incomplete_details"] = None
    return result


def _decode_jwt_exp(authorization: str) -> float | None:
    token = authorization.split(None, 1)[1]
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        padding = "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
        expiration = payload.get("exp")
        return float(expiration) if isinstance(expiration, (int, float)) else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _windows_dpapi_transform(data: bytes, *, decrypt: bool) -> bytes:
    if sys.platform != "win32":
        raise RuntimeError("secure Excel session persistence requires Windows DPAPI")

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    input_buffer = ctypes.create_string_buffer(data)
    input_blob = _DataBlob(
        len(data),
        ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_byte)),
    )
    output_blob = _DataBlob()
    flags = 0x1  # CRYPTPROTECT_UI_FORBIDDEN
    if decrypt:
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        succeeded = crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            None,
            None,
            None,
            None,
            flags,
            ctypes.byref(output_blob),
        )
    else:
        description = "excel-codex-bridge GPT Excel session"
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptProtectData.restype = wintypes.BOOL
        succeeded = crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            description,
            None,
            None,
            None,
            flags,
            ctypes.byref(output_blob),
        )
    if not succeeded:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))


def _protect_windows_data(data: bytes) -> bytes:
    return _windows_dpapi_transform(data, decrypt=False)


def _unprotect_windows_data(data: bytes) -> bytes:
    return _windows_dpapi_transform(data, decrypt=True)


class ExcelSessionStore:
    def __init__(self, persistence_file: str | None = None):
        self._lock = threading.Lock()
        self._headers: dict[str, str] = {}
        self._tools_version_id: str | None = None
        self._configured_at: float | None = None
        self._expires_at: float | None = None
        self._persistence_file = persistence_file
        self._persistence_error = ""

    def configure(
        self,
        raw_headers: object,
        *,
        tools_version_id: object = None,
        persist: bool = True,
        allow_expired: bool = False,
    ) -> dict[str, object]:
        if not isinstance(raw_headers, dict):
            raise ValueError("headers must be a JSON object")
        if tools_version_id is not None and (
            not isinstance(tools_version_id, str)
            or not _TOOLS_VERSION_PATTERN.fullmatch(tools_version_id.strip())
        ):
            raise ValueError("tools_version_id is not a valid Basispoints version ID")
        normalized_tools_version_id = (
            tools_version_id.strip() if isinstance(tools_version_id, str) else None
        )

        headers: dict[str, str] = {}
        for raw_name, raw_value in raw_headers.items():
            if not isinstance(raw_name, str) or not isinstance(raw_value, str):
                continue
            name = raw_name.strip().lower()
            value = raw_value.strip()
            if name in _ALLOWED_CAPTURED_HEADERS and value and len(value) <= 32_768:
                headers[name] = value

        authorization = headers.get("authorization", "")
        if not authorization.lower().startswith("bearer ") or len(authorization.split(None, 1)) != 2:
            raise ValueError("a Bearer authorization header is required")

        chatgpt_account = headers.get("chatgpt-account-id")
        openai_account = headers.get("x-openai-account-id")
        if not chatgpt_account and not openai_account:
            raise ValueError("a ChatGPT account ID header is required")
        if chatgpt_account and openai_account and chatgpt_account != openai_account:
            raise ValueError("captured account ID headers do not match")
        account_id = chatgpt_account or openai_account
        headers["chatgpt-account-id"] = account_id
        headers["x-openai-account-id"] = account_id

        for name, value in _DEFAULT_CLIENT_HEADERS.items():
            headers.setdefault(name, value)

        expires_at = _decode_jwt_exp(authorization)
        now = time.time()
        if expires_at is not None and expires_at <= now and not allow_expired:
            raise ValueError("the captured ChatGPT bearer token is already expired")

        with self._lock:
            self._headers = headers
            self._tools_version_id = normalized_tools_version_id
            self._configured_at = now
            self._expires_at = expires_at
            self._persistence_error = ""
        if persist and self._persistence_file:
            try:
                self._save()
            except (OSError, RuntimeError) as exc:
                with self._lock:
                    self._persistence_error = str(exc)
        return self.status()

    def clear(self) -> dict[str, object]:
        with self._lock:
            self._headers = {}
            self._tools_version_id = None
            self._configured_at = None
            self._expires_at = None
            self._persistence_error = ""
        if self._persistence_file:
            try:
                os.remove(self._persistence_file)
            except FileNotFoundError:
                pass
            except OSError as exc:
                with self._lock:
                    self._persistence_error = str(exc)
        return self.status()

    def load(self) -> dict[str, object]:
        if not self._persistence_file or not os.path.isfile(self._persistence_file):
            return self.status()
        try:
            with open(self._persistence_file, "rb") as handle:
                protected = handle.read()
            payload = json.loads(_unprotect_windows_data(protected))
            if not isinstance(payload, dict) or payload.get("version") != 1:
                raise ValueError("unsupported encrypted Excel session format")
            self.configure(
                payload.get("headers"),
                tools_version_id=payload.get("tools_version_id"),
                persist=False,
            )
        except (OSError, RuntimeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            with self._lock:
                self._headers = {}
                self._tools_version_id = None
                self._configured_at = None
                self._expires_at = None
                self._persistence_error = str(exc)
        return self.status()

    def _save(self) -> None:
        if not self._persistence_file:
            return
        with self._lock:
            payload = {
                "version": 1,
                "headers": dict(self._headers),
                "tools_version_id": self._tools_version_id,
            }
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        protected = _protect_windows_data(raw)
        directory = os.path.dirname(self._persistence_file)
        os.makedirs(directory, exist_ok=True)
        file_descriptor, temporary_path = tempfile.mkstemp(
            prefix=".excel-session-",
            suffix=".tmp",
            dir=directory,
        )
        try:
            with os.fdopen(file_descriptor, "wb") as handle:
                handle.write(protected)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self._persistence_file)
        except Exception:
            try:
                os.remove(temporary_path)
            except OSError:
                pass
            raise

    def status(self) -> dict[str, object]:
        with self._lock:
            configured = bool(self._headers)
            tools_version_id = self._tools_version_id
            configured_at = self._configured_at
            expires_at = self._expires_at
            persistence_error = self._persistence_error
        expired = expires_at is not None and expires_at <= time.time()
        return {
            "configured": configured,
            "expired": expired,
            "configured_at": configured_at,
            "expires_at": expires_at,
            "model": MODEL_ID,
            "upstream_model": UPSTREAM_MODEL,
            "upstream_url": RESPONSES_URL,
            "tools_version_id": tools_version_id,
            "storage": (
                "memory-and-windows-dpapi"
                if self._persistence_file and os.path.isfile(self._persistence_file)
                else "memory-only"
            ),
            "persistence_supported": sys.platform == "win32",
            "persisted": bool(
                self._persistence_file
                and os.path.isfile(self._persistence_file)
            ),
            "persistence": "windows-dpapi" if sys.platform == "win32" else "unavailable",
            "persistence_error": persistence_error,
        }

    def tools_version_id(self) -> str | None:
        with self._lock:
            return self._tools_version_id

    def request_headers(self, *, stream: bool) -> dict[str, str]:
        with self._lock:
            headers = dict(self._headers)
            expires_at = self._expires_at
        if not headers:
            if sys.platform == "darwin":
                raise RuntimeError(
                    "ChatGPT Excel sign-in was not found. Open the ChatGPT task pane in Excel and sign in."
                )
            raise RuntimeError(
                "GPT Excel is not configured. Read the cached session from the signed-in Excel add-in, then retry."
            )
        if expires_at is not None and expires_at <= time.time():
            if sys.platform == "darwin":
                raise RuntimeError(
                    "The ChatGPT Excel token has expired. Refresh the ChatGPT Excel task pane, then retry."
                )
            raise RuntimeError(
                "The GPT Excel session has expired. Refresh the signed-in Excel add-in session, then read it again."
            )
        headers.update(
            {
                "accept": "text/event-stream" if stream else "application/json",
                "accept-encoding": "identity",
                "content-type": "application/json",
                "origin": "https://bps.openai.com",
            }
        )
        return headers


def _message_item(role: str, text: str) -> dict:
    content_type = "output_text" if role == "assistant" else "input_text"
    return {
        "type": "message",
        "role": role,
        "content": [{"type": content_type, "text": text}],
    }


def _item_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for part in value:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "".join(parts)
    return ""


def _sealed(text: object) -> bool:
    return isinstance(text, str) and _SEALED.fullmatch(text.strip()) is not None


def _readable_parts(parts: list, *, sealed_too: bool = False) -> list:
    """``parts`` with ``encrypted_content`` that is plain text as ``input_text``.

    Codex labels a message the model sent another agent as encrypted unless
    the call said its arguments were plain (bridges up to 0.5.11 did not), and
    keeps it so in the conversation.  ``sealed_too`` also replaces what really
    is encrypted, which only the backend that encrypted it can read.
    """
    readable = []
    for part in parts:
        if isinstance(part, dict) and part.get("type") == "encrypted_content":
            text = part.get("encrypted_content")
            if not _sealed(text):
                part = {"type": "input_text", "text": text if isinstance(text, str) else ""}
            elif sealed_too:
                part = {"type": "input_text", "text": _SEALED_ELSEWHERE}
        readable.append(part)
    return readable


def _decoded_arguments(value: object) -> object:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


def _transport_failure(
    item: dict,
    allowed_tools: dict[str, str] | None,
    specs: dict[str, dict] | None = None,
) -> str:
    """Why a run_officejs call of the model's could not become one of Codex's tools."""
    arguments = _decoded_arguments(item.get("arguments"))
    if not isinstance(arguments, dict):
        return "its arguments were not a JSON object"
    envelope = _decode_transport_code(arguments.get("code"))
    wrappers = 0
    while envelope is not None and _is_transport_name(envelope.get("name")):
        wrappers += 1
        nested = _decoded_arguments(envelope.get("arguments"))
        if not isinstance(nested, dict):
            return "a run_officejs wrapper inside its code field had no JSON object as arguments"
        envelope = _decode_transport_code(nested.get("code"))
    if envelope is None:
        return "its code field did not hold a JSON object"
    if wrappers > 2:
        return "it wrapped run_officejs inside run_officejs more than twice"
    name = envelope.get("name")
    if not isinstance(name, str) or not name.strip():
        return "the object in its code field named no tool"
    name = name.strip()
    tools = allowed_tools or {}
    tool = _original_client_tool_name(name, tools)
    if tool is None:
        namespaced = sorted(key for key in tools if key.rsplit(".", 1)[-1] == name)
        if namespaced:
            return f"{name} is not a tool in the catalog; the catalog calls it {namespaced[0]}"
        if _code_mode(tools):
            return (
                f"{name} is not a tool in the catalog: Codex runs in code mode here, and the other "
                "tools are called from JavaScript in exec's input, for example code equal to "
                + CODE_MODE_EXAMPLE
            )
        if name in CODE_MODE_TOOLS:
            shell = next((key for key in ("exec_command", "shell_command", "shell") if key in tools), None)
            return (
                f"{name} is not a tool in the catalog: it belongs to Codex's code mode, which this "
                "request does not use; call the catalog's tools directly"
                + (f" ({shell} for shell commands)" if shell else "")
            )
        return f"{name} is not a tool in the catalog"
    if tools[tool] == "custom":
        if not isinstance(envelope.get("input"), str):
            return f"{tool} is a custom tool: its text goes in input, as a string, not in arguments"
        return _MALFORMED_TRANSPORT
    if not isinstance(_decoded_arguments(envelope.get("arguments")), dict):
        return f"the arguments for {tool} were not a JSON object"
    reason = f"its arguments did not match the parameters of {tool}"
    info = (specs or {}).get(tool)
    if info is not None and _summarized(info["namespace"]):
        # The catalog only summarizes this tool; here is all of it.
        definition = _catalog_entry(tool, info["namespace"], info["type"], info["spec"], full=True)
        reason += ", which is defined as " + json.dumps(definition, separators=(",", ":"), ensure_ascii=False)
    return reason


def _normalized_tool_output(
    item: dict,
    call_origins: dict[str, str],
    transport_failures: dict[str, str] | None = None,
) -> dict:
    call_id = item.get("call_id")
    origin = call_origins.get(call_id) if isinstance(call_id, str) else None
    normalized = item
    if isinstance(normalized.get("output"), list):
        normalized = {**normalized, "output": _readable_parts(normalized["output"])}
    if origin == "update_plan":
        # The Basispoints update_plan executor returns this object. Codex's
        # client-side status tool instead returns the display string
        # "Plan updated"; replaying that string leaves the server-native tool
        # state unresolved and makes the model plan again.
        normalized = {**normalized, "output": '{"status":"ok"}'}
    if (
        origin == CLIENT_TOOL_TRANSPORT_NAME
        and isinstance(call_id, str)
        and call_id
        and normalized.get("type") == "custom_tool_call_output"
    ):
        normalized = {**normalized, "type": "function_call_output"}
    if (
        isinstance(call_id, str)
        and call_id
        and normalized.get("type") == "function_call_output"
    ):
        canonical_id = responses_replay_ids.function_item_id(call_id)
        if normalized.get("id") != canonical_id:
            normalized = {**normalized, "id": canonical_id}
    output_text = _item_text(normalized.get("output"))
    if (
        origin == CLIENT_TOOL_TRANSPORT_NAME
        and output_text.strip().lower().startswith("unsupported call: run_officejs")
    ):
        reason = (transport_failures or {}).get(call_id, _MALFORMED_TRANSPORT)
        return {**normalized, "output": _TRANSPORT_RETRY_GUIDANCE.format(reason=reason)}
    if not output_text.strip() and isinstance(
        normalized.get("output"), (str, type(None))
    ):
        # A blank body reads as a failed call and provokes retries; make
        # success explicit.
        return {**normalized, "output": "(tool call succeeded with no output)"}
    return normalized


def _fallback_transport_call(item: dict) -> dict:
    """Rebuild a transport call if the proxy restarted between call and result."""
    # The catalog names a namespaced tool (collaboration.spawn_agent) the way the model must.
    namespace = item.get("namespace")
    name = _client_tool_key(
        str(item.get("name") or ""), namespace if isinstance(namespace, str) and namespace else None
    )
    if item.get("type") == TOOL_SEARCH_CALL:
        name = TOOL_SEARCH
    if item.get("type") == "custom_tool_call":
        envelope: dict[str, object] = {
            "name": name,
            "input": item.get("input") if isinstance(item.get("input"), str) else "",
        }
    else:
        arguments = item.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        envelope = {"name": name, "arguments": arguments}
    call_id = str(item.get("call_id") or f"{NATIVE_FALLBACK_CALL_ID_PREFIX}{uuid4().hex}")
    native_arguments = {
        "summary": f"Run client tool {name}",
        "extended_summary": f"Relay {name} through the external Codex client",
        "code": json.dumps(envelope, separators=(",", ":"), ensure_ascii=False),
        "destructive": False,
        "references": [],
    }
    return {
        "type": "function_call",
        "id": responses_replay_ids.function_item_id(call_id),
        "call_id": call_id,
        "name": CLIENT_TOOL_TRANSPORT_NAME,
        "arguments": json.dumps(
            native_arguments,
            separators=(",", ":"),
            ensure_ascii=False,
        ),
        "status": "completed",
    }


def _strip_client_only_item_metadata(item: dict) -> dict:
    """Remove caller transport metadata that destabilizes prompt caching.

    Codex stamps every input item with an
    ``internal_chat_message_metadata_passthrough.turn_id``. The value is not
    part of the Responses item vocabulary Basispoints needs, and identical
    developer/environment messages receive a different value in every new
    conversation. Forwarding it therefore makes byte-identical prompt content
    diverge immediately after the cached tool catalog.

    Native calls remembered from the Basispoints response bypass this helper
    and are replayed exactly, preserving the server item identity required by
    encrypted reasoning.  An empty ``encrypted_function_args`` is the bridge's
    own mark on the calls it gave Codex, not the backend's.
    """
    plain_mark = item.get(PLAIN_ARGUMENTS) == []
    if "internal_chat_message_metadata_passthrough" not in item and not plain_mark:
        return item
    sanitized = dict(item)
    sanitized.pop("internal_chat_message_metadata_passthrough", None)
    if plain_mark:
        sanitized.pop(PLAIN_ARGUMENTS)
    return sanitized


def _search_output_text(item: dict) -> str:
    """What a tool_search found, for the model: the full definition of each tool it loaded."""
    tools = item.get("tools") if isinstance(item.get("tools"), list) else []
    catalog = _catalog_json(tools, full=True)
    if catalog == "[]":
        return "No matching tools found."
    return (
        "tool_search loaded these tools; call them through run_officejs like catalog tools, "
        "by the name given here:\n" + catalog
    )


def translate_input_items(
    raw_input: object,
    allowed_tools: dict[str, str] | None = None,
    specs: dict[str, dict] | None = None,
) -> list:
    """Map Codex Responses input items onto the Excel wire vocabulary.

    Tool-call history keeps the native Responses items that Basispoints needs
    for its state machine. New client calls use Basispoints' declared
    ``run_officejs`` function as an intercepted transport. The original native
    item is retained in a bounded cache because Codex drops its server item ID
    when submitting the tool result; replaying only the name and call_id makes
    encrypted reasoning treat the result as unrelated. Native update_plan
    results are translated from Codex's display text to the ``{"status":"ok"}``
    object returned by Excel's real executor. Calls created by older proxy
    versions retain their namespaced marker representation.
    Codex's tool_search calls and results become a run_officejs call and its
    output, the result listing the tools the search loaded.
    Reasoning items that carry ``encrypted_content`` (which the upstream issues
    by default) are replayed unchanged for turn-to-turn continuity; bare
    reasoning items are dropped because with ``store: false`` the upstream
    rejects them. The rendering is deterministic so replayed turns serialize
    identically on every request and keep the upstream prompt-cache prefix
    stable.
    """
    if isinstance(raw_input, str):
        return [_message_item("user", raw_input)]
    if not isinstance(raw_input, list):
        return []

    call_origins: dict[str, str] = {}
    transport_failures: dict[str, str] = {}
    result: list = []
    for item in raw_input:
        if not isinstance(item, dict):
            continue
        item = _strip_client_only_item_metadata(item)
        item_type = str(item.get("type") or "").strip().lower()
        if item_type in {"function_call", "custom_tool_call"}:
            name = item.get("name")
            call_id = item.get("call_id")
            marker_relay = (
                isinstance(call_id, str)
                and call_id.startswith(CLIENT_MARKER_CALL_ID_PREFIX)
            )
            remembered = _remembered_native_call(call_id)
            if remembered is not None:
                native_name = remembered.get("name")
                if isinstance(call_id, str) and isinstance(native_name, str):
                    call_origins[call_id] = native_name
                result.append(remembered)
            elif isinstance(name, str) and name and marker_relay:
                upstream_name = relay_tool_name(name)
                if isinstance(call_id, str):
                    call_origins[call_id] = upstream_name
                result.append({**item, "name": upstream_name})
            elif _is_transport_name(name):
                # A relay call of the model's that did not become a Codex tool
                # reached Codex as it was; wrapping it again would nest it.
                if isinstance(call_id, str):
                    call_origins[call_id] = CLIENT_TOOL_TRANSPORT_NAME
                    transport_failures[call_id] = _transport_failure(item, allowed_tools, specs)
                leaked = {
                    **item,
                    "type": "function_call",
                    "name": CLIENT_TOOL_TRANSPORT_NAME,
                    "id": item.get("id") or responses_replay_ids.function_item_id(str(call_id or "")),
                }
                leaked.pop("namespace", None)
                result.append(leaked)
            elif isinstance(name, str) and name:
                if name == "update_plan":
                    if isinstance(call_id, str):
                        call_origins[call_id] = name
                    result.append(
                        {
                            **item,
                            "arguments": _restore_native_function_arguments(
                                name,
                                item.get("arguments"),
                            ),
                        }
                    )
                else:
                    fallback = _fallback_transport_call(item)
                    if isinstance(call_id, str):
                        call_origins[call_id] = CLIENT_TOOL_TRANSPORT_NAME
                    result.append(fallback)
            else:
                result.append(item)
            continue
        if item_type in {"function_call_output", "custom_tool_call_output"}:
            result.append(_normalized_tool_output(item, call_origins, transport_failures))
            continue
        if item_type == TOOL_SEARCH_CALL:
            call_id = item.get("call_id")
            remembered = _remembered_native_call(call_id)
            result.append(remembered if remembered is not None else _fallback_transport_call(item))
            continue
        if item_type == TOOL_SEARCH_OUTPUT:
            call_id = item.get("call_id")
            if isinstance(call_id, str) and call_id:
                result.append(
                    {
                        "type": "function_call_output",
                        "id": responses_replay_ids.function_item_id(call_id),
                        "call_id": call_id,
                        "output": _search_output_text(item),
                    }
                )
            continue
        if item_type == "agent_message" and isinstance(item.get("content"), list):
            result.append({**item, "content": _readable_parts(item["content"])})
            continue
        if item_type == "reasoning":
            encrypted = item.get("encrypted_content")
            if isinstance(encrypted, str) and encrypted:
                result.append(
                    {
                        "type": "reasoning",
                        "summary": [],
                        "encrypted_content": encrypted,
                    }
                )
            continue
        if item_type == "item_reference":
            continue
        result.append(item)
    return result


def without_sealed_content(body: dict) -> dict:
    """``body`` without what another backend encrypted; ``body`` itself when it has none.

    Encrypted reasoning items are left out.  An encrypted message between
    agents, or in a tool's output, is replaced by a note saying so, since the
    conversation has to keep the item.
    """
    items = body.get("input")
    if not isinstance(items, list):
        return body
    changed = False
    kept = []
    for item in items:
        if isinstance(item, dict):
            if item.get("type") == "reasoning" and item.get("encrypted_content"):
                changed = True
                continue
            key = "content" if item.get("type") == "agent_message" else "output"
            parts = item.get(key)
            if isinstance(parts, list):
                readable = _readable_parts(parts, sealed_too=True)
                if readable != parts:
                    changed = True
                    item = {**item, key: readable}
        kept.append(item)
    return {**body, "input": kept} if changed else body


def _conversation_fingerprint(input_items: list) -> str:
    """Stable conversation identity for clients that send no cache key.

    The first input item is the root of the conversation and does not change
    as turns are appended, so hashing it keeps one identity per conversation
    without inventing a random one per request.
    """
    for item in input_items:
        if isinstance(item, dict):
            rendered = json.dumps(item, sort_keys=True, separators=(",", ":"))
            return hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    return "anonymous"


def _cache_key(source: dict) -> str | None:
    for key in ("prompt_cache_key", "promptCacheKey", "session_id", "sessionId"):
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    # Codex carries its stable root session in client_metadata. Prefer it only
    # as a fallback, leaving an explicit caller cache key authoritative.
    client_metadata = source.get("client_metadata")
    if isinstance(client_metadata, dict):
        for key in ("session_id", "sessionId"):
            value = client_metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _agent_turn_state(raw_input: object) -> tuple[str, str]:
    """Return a stable user-turn fingerprint and Excel agent iteration.

    The Excel add-in holds ``turn_id`` constant while it executes any number of
    tools for one user message. Only ``agent_iteration`` advances. Treating
    every tool output as a new turn makes Basispoints discard the prior plan
    state and start planning again.
    """
    if isinstance(raw_input, str):
        rendered = json.dumps(raw_input, ensure_ascii=False)
        return hashlib.sha256(rendered.encode("utf-8")).hexdigest(), "1"
    if not isinstance(raw_input, list):
        return "anonymous", "1"

    last_user_index = -1
    for index, item in enumerate(raw_input):
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        if role == "user":
            last_user_index = index

    turn_prefix = (
        raw_input[: last_user_index + 1]
        if last_user_index >= 0
        else raw_input[:1]
    )
    rendered = json.dumps(
        turn_prefix,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    fingerprint = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    # One iteration per round of tool results: parallel calls come back together.
    rounds = 0
    in_results = False
    for item in raw_input[last_user_index + 1 :]:
        is_result = isinstance(item, dict) and item.get("type") in {
            "function_call_output",
            "custom_tool_call_output",
            TOOL_SEARCH_OUTPUT,
        }
        if is_result and not in_results:
            rounds += 1
        in_results = is_result
    return fingerprint, str(rounds + 1)


def _append_before_terminal_compaction_trigger(
    input_items: list,
    injected_items: list,
) -> list:
    """Insert proxy-authored items without displacing a compact trigger."""
    if (
        input_items
        and isinstance(input_items[-1], dict)
        and input_items[-1].get("type") == "compaction_trigger"
    ):
        return input_items[:-1] + injected_items + [input_items[-1]]
    return input_items + injected_items


def prepare_responses_body(
    source: dict,
    *,
    tools_version_id: str | None = None,
    identity_input: object = None,
) -> dict:
    """Translate a standard Responses request to the Excel add-in wire shape.

    ``identity_input``: the input as the client sent it, when ``source`` has
    been rewritten since (pictures swapped for uploaded file ids or notes,
    which can change from one attempt to the next).  Task and turn identity
    come from it, so a turn keeps its ``turn_id`` however its pictures went in.
    """
    output: dict[str, object] = {
        "model": upstream_model_for(source.get("model")),
        # Match the official Excel add-in wire shape. The add-in marks picker
        # choices as explicit so the Basispoints backend does not apply its
        # automatic/default model routing to an otherwise valid model slug.
        "model_selection": "explicit",
        "stream": bool(source.get("stream", False)),
        "store": False,
    }

    raw_input = source.get("input")
    input_items = translate_input_items(raw_input, client_tool_types(source), _client_tool_specs(source))
    identity = raw_input if identity_input is None else identity_input
    # Captured before the prologue is prepended: the injected instructions and
    # catalog are identical across conversations, so only the caller's own
    # first history item identifies this conversation. Use translated items:
    # Codex changes private per-turn metadata on a new user turn, but that
    # metadata must not create a new Excel task/session.
    history_root = _conversation_fingerprint(
        input_items if identity is raw_input else translate_input_items(identity, client_tool_types(source))
    )

    # Prompt layout is chosen for the upstream prompt cache: everything that is
    # stable across a conversation leads, so each turn only re-bills the newly
    # appended history plus the short trailing reminder. Putting the ~3.5k-token
    # catalog last instead (the old layout, still reachable through
    # GHCP_EXCEL_CATALOG_AT_PROMPT_END) forced the cache prefix to end at the
    # catalog's first byte and re-billed it on every request.
    prologue: list = []
    instructions = source.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        prologue.append(_message_item("developer", instructions))
    catalog = _message_item("developer", _client_tool_protocol_instructions(source))
    if CATALOG_AT_PROMPT_END:
        input_items = _append_before_terminal_compaction_trigger(
            prologue + input_items,
            [catalog],
        )
    else:
        prologue.append(catalog)
        reminder = _client_tool_protocol_reminder(source)
        if reminder:
            # Stable protocol instructions must precede conversation history.
            # A trailing reminder would become an insertion point on the next
            # turn and force the upstream cache to stop at that earlier byte.
            prologue.append(_message_item("developer", reminder))
        input_items = prologue + input_items
    output["input"] = input_items

    cache_key = _cache_key(source)
    if cache_key and FORWARD_PROMPT_CACHE_KEY:
        output["prompt_cache_key"] = cache_key

    reasoning = source.get("reasoning")
    requested_effort = (
        reasoning.get("effort")
        if isinstance(reasoning, dict)
        else source.get("reasoning_effort")
    )
    # Keep the public picker and the Basispoints wire value aligned. Unknown
    # or stale catalog values fall back to medium rather than producing a 422.
    output["reasoning_effort"] = _normalize_reasoning_effort(requested_effort) or "medium"

    context_management = source.get("context_management")
    output["context_management"] = (
        context_management
        if isinstance(context_management, list)
        else [{"type": "compaction", "compact_threshold": _default_compact_threshold(source.get("model"))}]
    )

    metadata: dict[str, str] = {}
    raw_metadata = source.get("metadata")
    if isinstance(raw_metadata, dict):
        for key, value in raw_metadata.items():
            if isinstance(key, str) and isinstance(value, (str, int, float, bool)):
                metadata[key[:64]] = str(value)[:512]
    turn_fingerprint, iteration = _agent_turn_state(identity)
    metadata.setdefault("agent_iteration", iteration)
    # Identifiers are derived, never random: the same client request must
    # serialize to the same bytes every time. A conversation keeps one task
    # identity across turns, and a retried turn keeps its turn identity, so a
    # retry is recognisable as the same turn rather than as new work.
    conversation = cache_key or history_root
    metadata.setdefault(
        "task_id",
        str(uuid5(NAMESPACE_URL, f"ghcp-proxy/gpt-excel/{conversation}")),
    )
    metadata.setdefault(
        "turn_id",
        str(
            uuid5(
                NAMESPACE_URL,
                f"ghcp-proxy/gpt-excel/{conversation}/turn/{turn_fingerprint}",
            )
        ),
    )
    if tools_version_id and _TOOLS_VERSION_PATTERN.fullmatch(tools_version_id):
        metadata[TOOLS_VERSION_METADATA_KEY] = tools_version_id
    output["metadata"] = metadata
    return output


excel_session_store = ExcelSessionStore(SESSION_FILE)

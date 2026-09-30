"""Codex wiring: a model catalog for the Excel aliases and the ``-c`` overrides.

The launcher passes everything on the Codex command line, so the user's
``~/.codex/config.toml`` is left alone; only ``excel-codex desktop`` edits it
(see ``desktop_config``).

Codex lists and resumes conversations by the provider they were started with.
When Codex is signed in, the bridge therefore stands in for Codex's own
``openai`` provider (``openai_base_url``) rather than adding one of its own,
and the catalog names models the way OpenAI does (``gpt-6-sol``, not
``gpt-6-sol-excel``): conversations started with or without the bridge then
carry on either way.  Without a sign-in Codex would ask for one before using
``openai``, so the bridge's own ``excel-bridge`` provider is used instead.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from . import excel_upstream

PROVIDER_ID = "excel-bridge"
PROVIDER_NAME = "Excel Bridge"
OPENAI_PROVIDER_ID = "openai"
DEFAULT_PORT = 8765
CATALOG_NAME = "codex-model-catalog.json"
# Codex offers its image tool (image_gen.imagegen) to a provider of its own only
# when the provider sets this header; the bridge answers the tool with the
# add-in's image endpoints and ignores the value.
IMAGE_TOOL_HEADER = ("x-openai-actor-authorization", "excel-codex-bridge")

# Codex requires base instructions for catalog models.  Codex's own templates
# describe tools the Excel path does not expose, so ship a compact prompt;
# AGENTS.md, environment and permission messages are still added by Codex.
BASE_INSTRUCTIONS = """\
You are Codex, a coding agent running in the user's terminal. You and the user share the same workspace and collaborate on software tasks.

- Work directly: inspect the repository with the shell tool, make focused edits with apply_patch, and verify with the project's own tests or builds when practical.
- Prefer `rg` for searching. Read a file before editing it. Keep changes minimal and consistent with the surrounding code; do not fix unrelated issues.
- Never revert or discard changes you did not make. Avoid destructive commands such as `git reset --hard` or `rm -rf` unless the user explicitly asks.
- For multi-step work, keep a short plan with the plan tool and update it as steps complete.
- If a command fails, read the error and adjust instead of repeating it unchanged.
- When done, reply concisely: what changed (with file paths), how it was verified, and anything left for the user. Use plain Markdown and do not paste whole files.
"""

CATALOG_ORDER = (
    "gpt-5.6-sol-excel", "gpt-5.6-sol-1m-excel",
    "gpt-6-sol-excel", "gpt-6-sol-1m-excel",
    "gpt-6-astra-excel", "gpt-6-astra-1m-excel",
    "gpt-6-luna-excel", "gpt-6-luna-1m-excel",
    "gpt-5.6-terra-excel", "gpt-5.6-terra-1m-excel",
    "gpt-5.6-luna-excel", "gpt-5.6-luna-1m-excel",
)



def codex_model(model: str) -> str:
    """The name Codex keeps for ``model``: OpenAI's own where there is one.

    ``gpt-6-sol-excel`` becomes ``gpt-6-sol``, which Codex's official sign-in
    serves too, so a conversation left on it carries on with the bridge off.
    A ``-1m`` alias has no official twin and stays as it is.
    """
    alias = excel_upstream.excel_model_id(model)
    if alias is None or alias.endswith(excel_upstream.LONG_CONTEXT_SUFFIX):
        return alias or model
    return excel_upstream.EXCEL_MODEL_UPSTREAMS[alias]


def official_model(model: str) -> str:
    """The model OpenAI serves in place of ``model``, for ``-1m`` aliases too.

    ``gpt-6-sol-1m-excel`` becomes ``gpt-6-sol``: what a conversation moved
    off the bridge's own provider carries on with once the bridge is off.
    """
    alias = excel_upstream.excel_model_id(model)
    return excel_upstream.EXCEL_MODEL_UPSTREAMS[alias] if alias else model


DEFAULT_MODEL = codex_model(excel_upstream.MODEL_ID)

_REASONING_LEVEL_DESCRIPTIONS = {
    "low": "Fast responses with lighter reasoning",
    "medium": "Balances speed and reasoning depth for everyday tasks",
    "high": "Greater reasoning depth for complex problems",
    "xhigh": "Extra high reasoning depth for complex problems",
}


def state_dir() -> Path:
    override = os.environ.get("EXCEL_BRIDGE_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "excel-codex-bridge"
    return Path.home() / ".excel-codex-bridge"


def catalog_payload() -> dict[str, object]:
    """Codex ``model_catalog_json`` entries for the Excel aliases.

    Each is listed under ``codex_model``'s name.  The old ``*-excel`` names
    stay in, hidden from the picker, for conversations started with them.
    """
    listed = [(codex_model(model_id), model_id, "list") for model_id in CATALOG_ORDER]
    hidden = [(model_id, model_id, "hide") for model_id in CATALOG_ORDER if codex_model(model_id) != model_id]
    models = []
    for priority, (slug, model_id, visibility) in enumerate(listed + hidden):
        caps = excel_upstream.LOCAL_MODEL_CAPABILITIES[model_id]
        context_window = int(caps["context_window"])
        # Codex compacts at this many tokens; keep a build buffer under the window.
        auto_compact = min(int(caps["auto_compact_token_limit"]), context_window - 8000)
        models.append(
            {
                "slug": slug,
                "display_name": caps["display_name"],
                "description": (
                    f"ChatGPT Excel add-in session · {context_window:,} token context · "
                    "counts against your ChatGPT plan, not API billing."
                ),
                "default_reasoning_level": "medium",
                "supported_reasoning_levels": [
                    {"effort": effort, "description": _REASONING_LEVEL_DESCRIPTIONS[effort]}
                    for effort in excel_upstream.EXCEL_REASONING_EFFORTS
                ],
                "shell_type": "shell_command",
                "visibility": visibility,
                "supported_in_api": True,
                "priority": priority,
                "additional_speed_tiers": [],
                "availability_nux": None,
                "upgrade": None,
                "base_instructions": BASE_INSTRUCTIONS,
                "model_messages": None,
                "supports_reasoning_summaries": True,
                "default_reasoning_summary": "auto",
                "support_verbosity": True,
                "default_verbosity": "low",
                "apply_patch_tool_type": "freeform",
                "web_search_tool_type": "text",
                "truncation_policy": {"mode": "bytes", "limit": 10000},
                "supports_parallel_tool_calls": True,
                "supports_image_detail_original": False,
                "context_window": context_window,
                "max_context_window": int(caps["max_context_window"]),
                "auto_compact_token_limit": auto_compact,
                "effective_context_window_percent": 95,
                "experimental_supported_tools": [],
                # Codex desktop refuses pasted pictures for a model without "image" here.
                "input_modalities": list(caps["input_modalities"]),
                "supports_search_tool": False,
            }
        )
    return {"models": models}


def write_catalog(directory: Path | None = None) -> Path:
    directory = directory or state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / CATALOG_NAME
    content = json.dumps(catalog_payload(), indent=2, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".catalog-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def _toml_string(value: str) -> str:
    # TOML literal strings keep Windows backslashes intact; fall back to a
    # basic string only when the value itself contains a single quote.
    if "'" not in value and "\n" not in value:
        return f"'{value}'"
    return json.dumps(value)


def base_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/v1"


def http_headers() -> str:
    name, value = IMAGE_TOOL_HEADER
    return f"{{ {json.dumps(name)} = {json.dumps(value)} }}"


def codex_signed_in(home: Path | None = None) -> bool:
    """Whether Codex has a sign-in of its own, so it can use its ``openai`` provider.

    Only looks at what kind of sign-in ``auth.json`` holds; no token is read out.
    """
    if home is None:
        override = os.environ.get("CODEX_HOME", "").strip()
        home = Path(override).expanduser() if override else Path.home() / ".codex"
    try:
        data = json.loads((home / "auth.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    tokens = data.get("tokens")
    access = tokens.get("access_token") if isinstance(tokens, dict) else None
    key = data.get("OPENAI_API_KEY")
    return any(isinstance(value, str) and value.strip() for value in (access, key))


def provider_for(shared: bool) -> str:
    return OPENAI_PROVIDER_ID if shared else PROVIDER_ID


def codex_overrides(port: int, catalog: Path, model: str = DEFAULT_MODEL, *, shared: bool = False) -> list[str]:
    """``-c`` arguments that route one Codex session through the bridge.

    ``shared`` points Codex's own ``openai`` provider at the bridge; the
    ``excel-bridge`` provider is defined either way, for conversations started
    with it.
    """
    prefix = f"model_providers.{PROVIDER_ID}"
    pairs = [
        ("model_provider", _toml_string(provider_for(shared))),
        *([("openai_base_url", _toml_string(base_url(port)))] if shared else []),
        (f"{prefix}.name", _toml_string(PROVIDER_NAME)),
        (f"{prefix}.base_url", _toml_string(base_url(port))),
        (f"{prefix}.wire_api", _toml_string("responses")),
        (f"{prefix}.http_headers", http_headers()),
        ("model_catalog_json", _toml_string(str(catalog))),
        ("model", _toml_string(codex_model(model))),
    ]
    args: list[str] = []
    for key, value in pairs:
        args += ["-c", f"{key}={value}"]
    return args


# Subcommands that talk to the model.  Codex drops root-level ``-c`` values
# when the subcommand is given its own ``-c``, so ours go after the subcommand.
_MODEL_SUBCOMMANDS = {"exec", "e", "review", "resume", "fork"}


def codex_command(codex: str, overrides: list[str], user_args: list[str]) -> list[str]:
    if user_args and user_args[0] in _MODEL_SUBCOMMANDS:
        return [codex, user_args[0], *overrides, *user_args[1:]]
    return [codex, *overrides, *user_args]


def config_snippet(port: int, catalog: Path, model: str = DEFAULT_MODEL) -> str:
    """config.toml text for clients that cannot take ``-c`` (IDE/desktop app)."""
    return (
        "# excel-codex-bridge: keep `excel-codex serve` running while using this\n"
        f"model_provider = {_toml_string(PROVIDER_ID)}\n"
        f"model = {_toml_string(codex_model(model))}\n"
        f"model_catalog_json = {_toml_string(str(catalog))}\n"
        "\n"
        f"[model_providers.{PROVIDER_ID}]\n"
        f"name = {_toml_string(PROVIDER_NAME)}\n"
        f"base_url = {_toml_string(base_url(port))}\n"
        'wire_api = "responses"\n'
        f"http_headers = {http_headers()}\n"
    )

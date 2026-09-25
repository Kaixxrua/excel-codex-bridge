"""Codex wiring: a model catalog for the Excel aliases and the ``-c`` overrides.

The launcher passes everything on the Codex command line, so the user's
``~/.codex/config.toml`` is left alone; only ``excel-codex desktop`` edits it
(see ``desktop_config``).
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
DEFAULT_MODEL = excel_upstream.MODEL_ID
DEFAULT_PORT = 8765
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

CATALOG_ORDER = ("gpt-5.6-sol-excel", "gpt-6-astra-excel", "gpt-5.6-terra-excel", "gpt-5.6-luna-excel")

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
    """Codex ``model_catalog_json`` entries for the Excel aliases."""
    models = []
    for priority, model_id in enumerate(CATALOG_ORDER):
        caps = excel_upstream.LOCAL_MODEL_CAPABILITIES[model_id]
        context_window = int(caps["context_window"])
        # Codex compacts at this many tokens; keep a build buffer under the window.
        auto_compact = min(int(caps["auto_compact_token_limit"]), context_window - 8000)
        models.append(
            {
                "slug": model_id,
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
                "visibility": "list",
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
    path = directory / "codex-model-catalog.json"
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


def codex_overrides(port: int, catalog: Path, model: str = DEFAULT_MODEL) -> list[str]:
    """``-c`` arguments that route one Codex session through the bridge."""
    prefix = f"model_providers.{PROVIDER_ID}"
    pairs = [
        ("model_provider", _toml_string(PROVIDER_ID)),
        (f"{prefix}.name", _toml_string(PROVIDER_NAME)),
        (f"{prefix}.base_url", _toml_string(base_url(port))),
        (f"{prefix}.wire_api", _toml_string("responses")),
        (f"{prefix}.http_headers", http_headers()),
        ("model_catalog_json", _toml_string(str(catalog))),
        ("model", _toml_string(model)),
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
        f"model = {_toml_string(model)}\n"
        f"model_catalog_json = {_toml_string(str(catalog))}\n"
        "\n"
        f"[model_providers.{PROVIDER_ID}]\n"
        f"name = {_toml_string(PROVIDER_NAME)}\n"
        f"base_url = {_toml_string(base_url(port))}\n"
        'wire_api = "responses"\n'
        f"http_headers = {http_headers()}\n"
    )

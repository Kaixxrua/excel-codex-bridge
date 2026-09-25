"""Point Codex's shared ``config.toml`` at the bridge, reversibly.

The Codex desktop app and IDE extension take no ``-c`` overrides, so
``excel-codex desktop`` writes two marked blocks into ``config.toml`` and
comments out the user's own ``model`` / ``model_provider`` /
``model_catalog_json`` lines with a marker prefix.  Removing the blocks and the
prefix gives back the original text byte for byte; ``enable`` checks that
before writing anything, and keeps a copy of the original next to it.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

from . import codex_config

TOP_START = "# >>> excel-codex-bridge: added by `excel-codex desktop`; `excel-codex desktop --off` removes it"
TOP_END = "# <<< excel-codex-bridge"
BOTTOM_START = "# >>> excel-codex-bridge provider"
BOTTOM_END = "# <<< excel-codex-bridge provider"
# Set on the bottom marker when the original text did not end with a newline.
NO_EOL_FLAG = " (original had no final newline)"
DISABLED_PREFIX = "# excel-codex-bridge disabled: "
BACKUP_SUFFIX = ".before-excel-codex"

_TABLE_HEADER = re.compile(r"\s*\[")
_OWN_KEYS = re.compile(r"\s*(model_provider|model|model_catalog_json)\s*=")
# The provider's table and any of its sub-tables, such as its http_headers.
_OWN_TABLE = re.compile(
    rf"\s*\[\s*model_providers\s*\.\s*([\"']?){re.escape(codex_config.PROVIDER_ID)}\1\s*[.\]]"
)
_BOM = "﻿"


def codex_home() -> Path:
    override = os.environ.get("CODEX_HOME", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".codex"


def config_path() -> Path:
    return codex_home() / "config.toml"


def _bare(line: str) -> str:
    return line.rstrip("\r\n")


def is_enabled(text: str) -> bool:
    return any(_bare(line) == TOP_START for line in text.splitlines())


def strip_managed(text: str) -> str:
    """Undo ``enable``: drop our blocks and restore the lines we commented out."""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    no_eol = False
    i = 0
    while i < len(lines):
        bare = _bare(lines[i])
        if bare == TOP_START or bare.startswith(BOTTOM_START):
            end_marker = TOP_END if bare == TOP_START else BOTTOM_END
            if bare.startswith(BOTTOM_START):
                no_eol = bare.endswith(NO_EOL_FLAG)
                if out and _bare(out[-1]) == "":
                    out.pop()  # the blank line enable put before the block
            end = next((j for j in range(i + 1, len(lines)) if _bare(lines[j]) == end_marker), None)
            i = i + 1 if end is None else end + 1
            if bare == TOP_START and i < len(lines) and _bare(lines[i]) == "":
                i += 1  # the blank line enable put after the block
            continue
        line = lines[i]
        out.append(line[len(DISABLED_PREFIX):] if line.startswith(DISABLED_PREFIX) else line)
        i += 1
    result = "".join(out)
    if no_eol:
        result = result[:-2] if result.endswith("\r\n") else result[:-1] if result.endswith("\n") else result
    return result


def _comment_out_conflicts(text: str) -> str:
    out = []
    top_level = True
    in_own_table = False
    for line in text.splitlines(keepends=True):
        if _TABLE_HEADER.match(line):
            top_level = False
            in_own_table = bool(_OWN_TABLE.match(line))
        if in_own_table or (top_level and _OWN_KEYS.match(line)):
            out.append(DISABLED_PREFIX + line)
        else:
            out.append(line)
    return "".join(out)


def enable(text: str, *, port: int, catalog: Path, model: str) -> str:
    """``text`` with the bridge made the default provider for every Codex client."""
    base = strip_managed(text)
    nl = "\r\n" if "\r\n" in base else "\n"
    body = _comment_out_conflicts(base)
    no_eol = bool(body) and not body.endswith("\n")
    toml = codex_config._toml_string
    top = [
        TOP_START,
        f"model_provider = {toml(codex_config.PROVIDER_ID)}",
        f"model = {toml(model)}",
        f"model_catalog_json = {toml(str(catalog))}",
        TOP_END,
    ]
    bottom = [
        BOTTOM_START + (NO_EOL_FLAG if no_eol else ""),
        f"[model_providers.{codex_config.PROVIDER_ID}]",
        f"name = {toml(codex_config.PROVIDER_NAME)}",
        f"base_url = {toml(codex_config.base_url(port))}",
        'wire_api = "responses"',
        f"http_headers = {codex_config.http_headers()}",
        BOTTOM_END,
    ]
    return nl.join(top) + nl + nl + body + (nl if no_eol else "") + nl + nl.join(bottom) + nl


def _parse(text: str) -> dict | None:
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10: skip the extra check
        return None
    return tomllib.loads(text)


def profile_override(text: str) -> str | None:
    """The active profile's name if it sets its own model or provider."""
    try:
        data = _parse(text)
    except ValueError:
        return None
    if not data or not isinstance(data.get("profile"), str):
        return None
    profile = data.get("profiles", {}).get(data["profile"], {})
    if isinstance(profile, dict) and ("model" in profile or "model_provider" in profile):
        return data["profile"]
    return None


def _read(path: Path) -> tuple[str, bool]:
    if not path.exists():
        return "", False
    text = path.read_bytes().decode("utf-8")
    return (text[1:], True) if text.startswith(_BOM) else (text, False)


def _write(path: Path, text: str, bom: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(((_BOM if bom else "") + text).encode("utf-8"))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class ConfigError(RuntimeError):
    pass


def enable_file(path: Path, *, port: int, catalog: Path, model: str) -> Path | None:
    """Enable in ``path``; returns the backup made of the original, if any."""
    text, bom = _read(path)
    new = enable(text, port=port, catalog=catalog, model=model)
    original = strip_managed(text)
    if strip_managed(new) != original:
        raise ConfigError(f"{path} has a layout this tool cannot undo exactly; edit it by hand instead")
    try:
        data = _parse(new)
    except ValueError as exc:
        raise ConfigError(f"{path} would not be valid TOML ({exc}); is it valid now?") from exc
    if data is not None and (
        data.get("model_provider") != codex_config.PROVIDER_ID
        or data.get("model_providers", {}).get(codex_config.PROVIDER_ID, {}).get("base_url")
        != codex_config.base_url(port)
    ):
        raise ConfigError(f"could not point {path} at the bridge")
    backup = None
    if path.exists() and not is_enabled(text):
        backup = path.with_name(path.name + BACKUP_SUFFIX)
        shutil.copy2(path, backup)
    _write(path, new, bom)
    return backup


def disable_file(path: Path) -> bool:
    """Undo ``enable_file``; False when there was nothing to undo."""
    text, bom = _read(path)
    if not is_enabled(text) and DISABLED_PREFIX not in text:
        return False
    _write(path, strip_managed(text), bom)
    return True

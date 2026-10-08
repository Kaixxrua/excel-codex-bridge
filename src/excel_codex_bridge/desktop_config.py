"""Point Codex's shared ``config.toml`` at the bridge, reversibly.

The Codex desktop app and IDE extension take no ``-c`` overrides, so
``excel-codex desktop`` writes two marked blocks into ``config.toml`` and
comments out the user's own ``model`` / ``model_provider`` /
``model_catalog_json`` / ``openai_base_url`` lines with a marker prefix.  Removing the blocks and the
prefix gives back the original text byte for byte; ``enable`` checks that
before writing anything, and keeps a copy of the original next to it.

It also turns off two Codex features that wait on chatgpt.com when signed in
with ChatGPT: apps (up to 30 s when a session starts) and remote plugin
suggestions (5 s on every turn, since failures are not remembered).  Through a
proxy node that is down, the desktop app sits on "loading" meanwhile.

``official_file`` (``excel-codex restore``) goes further: besides the marked
blocks it comments out the bridge's settings put in some other way, such as a
``print-config`` snippet pasted in by hand, so Codex is back on its own setup.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import codex_config, excel_upstream

TOP_START = "# >>> excel-codex-bridge: added by `excel-codex desktop`; `excel-codex desktop --off` removes it"
TOP_END = "# <<< excel-codex-bridge"
BOTTOM_START = "# >>> excel-codex-bridge provider"
BOTTOM_END = "# <<< excel-codex-bridge provider"
# Set on the bottom marker when the original text did not end with a newline.
NO_EOL_FLAG = " (original had no final newline)"
# Put inside the user's own [features] table, which the top block cannot add to.
FEATURES_START = "# >>> excel-codex-bridge features"
FEATURES_END = "# <<< excel-codex-bridge features"
DISABLED_PREFIX = "# excel-codex-bridge disabled: "
BACKUP_SUFFIX = ".before-excel-codex"
QUIET_FEATURES = ("apps", "remote_plugin")

_TABLE_HEADER = re.compile(r"\s*\[")
_OWN_KEYS = re.compile(r"\s*(model_provider|model|model_catalog_json|openai_base_url)\s*=")
# The provider's table and any of its sub-tables, such as its http_headers.
_OWN_TABLE = re.compile(
    rf"\s*\[\s*model_providers\s*\.\s*([\"']?){re.escape(codex_config.PROVIDER_ID)}\1\s*[.\]]"
)
_FEATURES_TABLE = re.compile(r"""\s*\[\s*(["']?)features\1\s*\]\s*(#.*)?$""")
_FEATURE_KEYS = re.compile(rf"""\s*(["']?)({'|'.join(QUIET_FEATURES)})\1\s*=""")
_ROOT_FEATURE_KEYS = re.compile(rf"""\s*(["']?)features\1\s*\.\s*(["']?)({'|'.join(QUIET_FEATURES)})\2\s*=""")
# `features = { ... }` cannot be added to; the features are left alone then.
_INLINE_FEATURES = re.compile(r"""\s*(["']?)features\1\s*=""")
_BOM = "﻿"
# Put before each line `official_file` takes out; nothing puts them back by itself.
RESTORE_PREFIX = "# excel-codex restore took out: "
RESTORE_BACKUP_SUFFIX = ".before-excel-codex-restore"
_PROFILE_TABLE = re.compile(r"""\s*\[\s*(["']?)profiles\1\s*\.""")
_OWN_DOTTED = re.compile(
    rf"""\s*(["']?)model_providers\1\s*\.\s*(["']?){re.escape(codex_config.PROVIDER_ID)}\2\s*\."""
)
_ROUTE_KEY = re.compile(r"""\s*(["']?)(model_provider|model|model_catalog_json|openai_base_url)\1\s*=""")
_STRING_VALUE = re.compile(r"""=\s*(?:"((?:[^"\\]|\\.)*)"|'([^']*)')\s*(?:#.*)?$""")
_LOOPBACK_URL = re.compile(r"https?://(127\.\d+\.\d+\.\d+|localhost|\[::1\])(:\d+)?(/|$)", re.IGNORECASE)
_BRIDGE_DIRS = {"excel-codex-bridge", ".excel-codex-bridge"}


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
        if bare == FEATURES_START:
            end = next((j for j in range(i + 1, len(lines)) if _bare(lines[j]) == FEATURES_END), None)
            i = i + 1 if end is None else end + 1
            continue
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


def _comment_out_conflicts(text: str, *, quiet_features: bool = False) -> str:
    out = []
    top_level = True
    in_own_table = in_features = False
    for line in text.splitlines(keepends=True):
        if _TABLE_HEADER.match(line):
            top_level = False
            in_own_table = bool(_OWN_TABLE.match(line))
            in_features = bool(_FEATURES_TABLE.match(line))
        feature = quiet_features and (
            (in_features and _FEATURE_KEYS.match(line)) or (top_level and _ROOT_FEATURE_KEYS.match(line))
        )
        if in_own_table or (top_level and _OWN_KEYS.match(line)) or feature:
            out.append(DISABLED_PREFIX + line)
        else:
            out.append(line)
    return "".join(out)


def _quiet_lines(prefix: str = "") -> list[str]:
    return [f"{prefix}{name} = false" for name in QUIET_FEATURES]


def _features_table(text: str) -> int | None:
    """Index of the line opening the ``[features]`` table, if there is one."""
    return next((i for i, line in enumerate(text.splitlines()) if _FEATURES_TABLE.match(line)), None)


def _inline_features(text: str) -> bool:
    for line in text.splitlines():
        if _TABLE_HEADER.match(line):
            return False
        if _INLINE_FEATURES.match(line):
            return True
    return False


def _into_features_table(body: str, index: int, nl: str) -> str:
    lines = body.splitlines(keepends=True)
    header = lines[index]
    block = nl.join([FEATURES_START, *_quiet_lines(), FEATURES_END])
    # A header on the last line without a newline: the block ends the text the same way.
    lines[index] = header + block + nl if header.endswith("\n") else header + nl + block
    return "".join(lines)


def enable(
    text: str, *, port: int, catalog: Path, model: str, shared: bool = False, quiet_features: bool = False,
    excel: bool = True
) -> str:
    """``text`` with the bridge made the default provider for every Codex client.

    ``shared`` points Codex's own ``openai`` provider at the bridge, so the
    conversation list is the same with the bridge on or off (see
    ``codex_config``).  The ``excel-bridge`` provider is defined either way.
    ``quiet_features`` turns off ``QUIET_FEATURES`` unless ``features`` is an
    inline table.
    """
    base = strip_managed(text)
    nl = "\r\n" if "\r\n" in base else "\n"
    quiet_features = quiet_features and not _inline_features(base)
    body = _comment_out_conflicts(base, quiet_features=quiet_features)
    table = _features_table(body) if quiet_features else None
    if table is not None:
        body = _into_features_table(body, table, nl)
    no_eol = bool(body) and not body.endswith("\n")
    toml = codex_config._toml_string
    top = [
        TOP_START,
        f"model_provider = {toml(codex_config.provider_for(shared))}",
        *([f"openai_base_url = {toml(codex_config.base_url(port))}"] if shared else []),
        f"model = {toml(codex_config.codex_model(model))}",
        f"model_catalog_json = {toml(str(catalog))}",
        *(_quiet_lines("features.") if quiet_features and table is None else []),
        TOP_END,
    ]
    bottom = [
        BOTTOM_START + (NO_EOL_FLAG if no_eol else ""),
        f"[model_providers.{codex_config.PROVIDER_ID}]",
        f"name = {toml(codex_config.PROVIDER_NAME)}",
        f"base_url = {toml(codex_config.base_url(port))}",
        'wire_api = "responses"',
        f"http_headers = {codex_config.http_headers() if excel else '{}'}",
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


def features_quiet(text: str) -> bool:
    """Whether ``text`` has ``QUIET_FEATURES`` turned off."""
    try:
        data = _parse(text)
    except ValueError:
        return False
    if data is None:  # Python 3.10: our own lines tell
        lines = {_bare(line) for line in text.splitlines()}
        return FEATURES_START in lines or set(_quiet_lines("features.")) <= lines
    features = data.get("features")
    return isinstance(features, dict) and all(features.get(name) is False for name in QUIET_FEATURES)


def _checked(path: Path, text: str, *, port: int, catalog: Path, model: str, shared: bool,
             quiet_features: bool, excel: bool = True) -> str:
    new = enable(text, port=port, catalog=catalog, model=model, shared=shared, quiet_features=quiet_features, excel=excel)
    original = strip_managed(text)
    if strip_managed(new) != original:
        raise ConfigError(f"{path} has a layout this tool cannot undo exactly; edit it by hand instead")
    try:
        data = _parse(new)
    except ValueError as exc:
        raise ConfigError(f"{path} would not be valid TOML ({exc}); is it valid now?") from exc
    if data is not None and (
        data.get("model_provider") != codex_config.provider_for(shared)
        or data.get("model_providers", {}).get(codex_config.PROVIDER_ID, {}).get("base_url")
        != codex_config.base_url(port)
        or (shared and data.get("openai_base_url") != codex_config.base_url(port))
    ):
        raise ConfigError(f"could not point {path} at the bridge")
    if quiet_features and not features_quiet(new):
        raise ConfigError(f"could not turn off Codex features in {path}")
    return new


def enable_file(
    path: Path, *, port: int, catalog: Path, model: str, shared: bool = False, quiet_features: bool = False,
    excel: bool = True
) -> Path | None:
    """Enable in ``path``; returns the backup made of the original, if any.

    Should turning the features off be what stands in the way, the config is
    written without it; ``features_quiet`` tells which way it went.
    """
    text, bom = _read(path)
    settings = dict(port=port, catalog=catalog, model=model, shared=shared, excel=excel)
    try:
        new = _checked(path, text, **settings, quiet_features=quiet_features)
    except ConfigError:
        if not quiet_features:
            raise
        new = _checked(path, text, **settings, quiet_features=False)
    backup = None
    if path.exists() and not is_enabled(text):
        backup = path.with_name(path.name + BACKUP_SUFFIX)
        shutil.copy2(path, backup)
    _write(path, new, bom)
    return backup


def disable_file(path: Path) -> bool:
    """Undo ``enable_file``; False when there was nothing to undo."""
    text, bom = _read(path)
    if not is_enabled(text) and DISABLED_PREFIX not in text and FEATURES_START not in text:
        return False
    _write(path, strip_managed(text), bom)
    return True


# ─── back to Codex's own setup ────────────────────────────────────────────────

@dataclass
class Restored:
    """What ``official_file`` did to a config."""

    # `excel-codex desktop`'s marked blocks came out, and the lines it had set aside went back.
    blocks: bool = False
    # The bridge's settings put in some other way, now commented out (as shown to the user).
    took_out: list[str] = field(default_factory=list)
    # The file as it was, saved before ``took_out``.
    backup: Path | None = None
    # The bridge's settings that could not be commented out without breaking the file.
    stuck: list[str] = field(default_factory=list)
    # Settings that are not the bridge's but still keep Codex off its own setup, such as a relay's.
    kept: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.blocks or bool(self.took_out)


def _string_value(line: str) -> str | None:
    """The string a one-line ``key = "value"`` sets, else None."""
    match = _STRING_VALUE.search(_bare(line))
    if match is None:
        return None
    if match.group(2) is not None:
        return match.group(2)
    try:
        return json.loads(f'"{match.group(1)}"')
    except ValueError:
        return match.group(1)


def _without_login(url: str) -> str:
    scheme, _, rest = url.partition("://")
    return f"{scheme}://{rest.rpartition('@')[2]}" if rest else url


def _bridge_catalog(value: str) -> bool:
    """Whether ``value`` is the model list the bridge writes (``codex_config.write_catalog``)."""
    parts = [part for part in re.split(r"[\\/]+", value.strip()) if part]
    names = (codex_config.CATALOG_NAME, "codex-native-model-catalog.json")
    if len(parts) >= 2 and parts[-1] in names and parts[-2].lower() in _BRIDGE_DIRS:
        return True
    actual = os.path.normcase(os.path.abspath(os.path.expanduser(value)))
    return any(actual == os.path.normcase(os.path.abspath(codex_config.state_dir() / name)) for name in names)


def _leftovers(text: str) -> tuple[list[int], set[int], list[str]]:
    """The bridge's lines in ``text`` (indexes; those in its provider's tables again on their own),
    and settings of others that keep Codex off its own setup.

    The bridge's: at the top level or in a profile, ``model_provider =
    "excel-bridge"``, one of its model names (``gpt-6-sol-excel``) and its model
    list; the ``excel-bridge`` provider's tables and dotted keys; and a
    loopback ``openai_base_url`` when any of those is there too, or when it is
    the bridge's default address.
    """
    lines = text.splitlines(keepends=True)
    ours: list[int] = []
    tables: set[int] = set()
    loopback: list[int] = []
    kept: list[str] = []
    where = "top"
    for index, line in enumerate(lines):
        if _TABLE_HEADER.match(line):
            where = "own" if _OWN_TABLE.match(line) else "profile" if _PROFILE_TABLE.match(line) else "other"
        if where == "own":
            if line.strip() and not line.lstrip().startswith("#"):
                ours.append(index)
                tables.add(index)
            continue
        if where == "other" or line.lstrip().startswith("#"):
            continue
        if where == "top" and _OWN_DOTTED.match(line):
            ours.append(index)
            continue
        key = _ROUTE_KEY.match(line)
        value = _string_value(line) if key else None
        if value is None:
            continue
        name = key.group(2)
        if name == "model_provider":
            if value == codex_config.PROVIDER_ID:
                ours.append(index)
            elif value != codex_config.OPENAI_PROVIDER_ID:
                kept.append(f"model_provider = {json.dumps(value)}")
        elif name == "model":
            if excel_upstream.excel_model_id(value):
                ours.append(index)
        elif name == "model_catalog_json":
            if _bridge_catalog(value):
                ours.append(index)
        elif where == "top":  # openai_base_url; profiles have none
            if _LOOPBACK_URL.match(value.strip()):
                loopback.append(index)
            else:
                kept.append(f"openai_base_url = {json.dumps(_without_login(value))}")
    default = codex_config.base_url(codex_config.DEFAULT_PORT)
    for index in loopback:
        value = _string_value(lines[index]) or ""
        if ours or value.strip().rstrip("/") == default:
            ours.append(index)
        else:
            kept.append(f"openai_base_url = {json.dumps(_without_login(value))}")
    return sorted(ours), tables, kept


def _shown(lines: list[str], indexes: list[int], tables: set[int]) -> list[str]:
    """``indexes`` of ``lines`` as told to the user: a table by its header, URLs without a login."""
    shown: list[str] = []
    for index in indexes:
        line = _bare(lines[index]).strip()
        key = _ROUTE_KEY.match(line)
        value = _string_value(line)
        if index in tables:
            if _TABLE_HEADER.match(line):
                shown.append(f"{line} (the whole table)")
        elif key and key.group(2) == "openai_base_url" and value is not None:
            shown.append(f"openai_base_url = {json.dumps(_without_login(value))}")
        elif _OWN_DOTTED.match(line):
            shown.append(f"{line.partition('=')[0].strip()} = ...")
        else:
            shown.append(line)
    return shown


def _official(text: str) -> bool:
    """Whether ``text`` is valid TOML that no longer sets the bridge up (True without tomllib)."""
    try:
        data = _parse(text)
    except ValueError:
        return False
    if data is None:
        return True
    providers = data.get("model_providers")
    return data.get("model_provider") != codex_config.PROVIDER_ID and not (
        isinstance(providers, dict) and codex_config.PROVIDER_ID in providers
    )


def official_file(path: Path) -> Restored:
    """Put ``path`` back on Codex's own setup (``excel-codex restore``).

    First what ``disable_file`` undoes, exactly; then the bridge's settings put
    in some other way are commented out with ``RESTORE_PREFIX``, once the file
    as it was is saved next to it.  Should commenting them out leave invalid
    TOML, or the bridge still set up, they are left as they are, in ``stuck``.
    """
    text, bom = _read(path)
    restored = Restored(blocks=is_enabled(text) or DISABLED_PREFIX in text or FEATURES_START in text)
    base = strip_managed(text) if restored.blocks else text
    ours, tables, restored.kept = _leftovers(base)
    new = base
    if ours:
        lines = base.splitlines(keepends=True)
        chosen = set(ours)
        candidate = "".join(RESTORE_PREFIX + line if i in chosen else line for i, line in enumerate(lines))
        if _official(candidate):
            new, restored.took_out = candidate, _shown(lines, ours, tables)
        else:
            restored.stuck = _shown(lines, ours, tables)
    if restored.took_out and path.exists():
        restored.backup = path.with_name(path.name + RESTORE_BACKUP_SUFFIX)
        shutil.copy2(path, restored.backup)
    if new != text:
        _write(path, new, bom)
    return restored

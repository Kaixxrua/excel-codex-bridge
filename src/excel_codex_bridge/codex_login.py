"""The ChatGPT sign-in that Codex keeps for itself, used as the bridge's session.

`codex login` saves a ChatGPT sign-in in ``$CODEX_HOME/auth.json``
(``~/.codex/auth.json`` by default).  Sending that sign-in to the Excel
backend, so that Excel need not be installed, is the idea of bps-local by
MIKUbiu (https://github.com/MIKUbiu/bps-local); this is a separate
implementation of it.  Should the backend refuse it, ``session`` falls back
to the Excel add-in's own session.

The file is only read, never written, and its token goes nowhere but the
Excel backend.  When Codex writes a new token there (after `codex login`, for
example), the bridge picks it up the next time it reads the file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .excel_session_capture import _jwt_payload

# Points the bridge at another auth.json; mostly for tests.
AUTH_FILE_ENV = "EXCEL_BRIDGE_CODEX_AUTH"
READER = "codex-auth-json"


def auth_path() -> Path:
    explicit = os.environ.get(AUTH_FILE_ENV, "").strip()
    if explicit:
        return Path(explicit).expanduser()
    home = os.environ.get("CODEX_HOME", "").strip()
    return (Path(home).expanduser() if home else Path.home() / ".codex") / "auth.json"


def signature(path: Path) -> tuple[int, int] | None:
    """Changes whenever Codex writes the file, e.g. after `codex login`."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def load(path: Path | None = None) -> dict[str, str]:
    """Request headers for the ChatGPT sign-in in Codex's auth.json."""
    path = path or auth_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"Codex is not signed in ({path} does not exist)") from None
    except json.JSONDecodeError:
        raise ValueError(f"{path} is not a Codex login file") from None
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a Codex login file")
    tokens = data.get("tokens")
    access_token = tokens.get("access_token") if isinstance(tokens, dict) else None
    if not isinstance(access_token, str) or not access_token.strip():
        if data.get("OPENAI_API_KEY"):
            raise ValueError("Codex is signed in with an API key; the Excel backend needs a ChatGPT sign-in")
        raise ValueError(f"no ChatGPT sign-in in {path}")
    access_token = access_token.strip()
    claims = _jwt_payload(access_token).get("https://api.openai.com/auth")
    claims = claims if isinstance(claims, dict) else {}
    account_id = tokens.get("account_id") or claims.get("chatgpt_account_id")
    if not isinstance(account_id, str) or not account_id:
        raise ValueError("the Codex sign-in has no ChatGPT account ID; sign in again with `codex login`")
    headers = {
        "authorization": f"Bearer {access_token}",
        "chatgpt-account-id": account_id,
        "x-openai-account-id": account_id,
        "x-basispoints-auth-mode": "chatgpt",
    }
    account_user_id = claims.get("chatgpt_account_user_id")
    if isinstance(account_user_id, str) and account_user_id:
        headers["x-openai-account-user-id"] = account_user_id
    return headers

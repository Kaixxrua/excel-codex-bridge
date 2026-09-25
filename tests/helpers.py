from __future__ import annotations

import base64
import json
import time
from pathlib import Path


def jwt_with_exp(exp: float, **claims) -> str:
    def encode(payload: dict) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode({'exp': int(exp), **claims})}.sig"


def session_headers(exp: float | None = None, account: str = "account-id") -> dict[str, str]:
    token = jwt_with_exp(exp if exp is not None else time.time() + 3600)
    return {"authorization": f"Bearer {token}", "chatgpt-account-id": account}


def storage_payload(exp: float, account: str = "account-id", **user_info) -> dict:
    return {
        "authMode": "chatgpt",
        "sessionInfo": {"access_token": jwt_with_exp(exp)},
        "userInfo": {"chatgpt_account_id": account, **user_info},
    }


# ─── WebView2 LocalStorage (LevelDB log) fixtures ────────────────────────────

SESSION_KEY = b"_https://bps.openai.com\x00\x01bps_auth_tokens"
LEVELDB_BLOCK = 32768


def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def write_batch(puts: list[tuple[bytes, bytes]], deletes: list[bytes] = (), seq: int = 1) -> bytes:
    body = bytearray(seq.to_bytes(8, "little") + (len(puts) + len(deletes)).to_bytes(4, "little"))
    for key in deletes:
        body += b"\x00" + varint(len(key)) + key
    for key, value in puts:
        body += b"\x01" + varint(len(key)) + key + varint(len(value)) + value
    return bytes(body)


def log_file(records: list[bytes]) -> bytes:
    """Frame records as a LevelDB log, splitting across 32 KiB blocks."""
    out = bytearray()
    for record in records:
        remaining = record
        first = True
        while True:
            space = LEVELDB_BLOCK - len(out) % LEVELDB_BLOCK
            if space < 7:
                out += b"\x00" * space
                space = LEVELDB_BLOCK
            chunk = remaining[: space - 7]
            remaining = remaining[len(chunk):]
            last = not remaining
            kind = 1 if first and last else 2 if first else 4 if last else 3
            out += b"\x00\x00\x00\x00" + len(chunk).to_bytes(2, "little") + bytes([kind]) + chunk
            first = False
            if last:
                break
    return bytes(out)


def webview_db(root: Path) -> Path:
    db = root / "Wef" / "EBWebView" / "Default" / "Local Storage" / "leveldb"
    db.mkdir(parents=True)
    return db


def write_webview_session(root: Path, exp: float, account: str = "account-id") -> Path:
    """Create a WebView2 root holding a signed-in add-in session."""
    value = b"\x01" + json.dumps(storage_payload(exp, account=account)).encode()
    db = webview_db(root)
    (db / "000003.log").write_bytes(log_file([write_batch([(SESSION_KEY, value)])]))
    return root


def write_codex_login(path: Path, exp: float, account: str | None = "codex-account", **claims) -> Path:
    """Write a Codex auth.json holding a ChatGPT sign-in, like `codex login` does."""
    auth = {"chatgpt_account_id": account or "claims-account", **claims}
    tokens = {
        "id_token": jwt_with_exp(exp),
        "access_token": jwt_with_exp(exp, **{"https://api.openai.com/auth": auth}),
        "refresh_token": "refresh-not-used",
    }
    if account:
        tokens["account_id"] = account
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "tokens": tokens}))
    return path

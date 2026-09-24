from __future__ import annotations

import base64
import json
import time


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

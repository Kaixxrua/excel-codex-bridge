"""Resolve only explicit credential references; public inventories contain hashes."""

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import shutil
import time

from .. import codex_config, codex_login
from ..native_upstream import BASE_URL, NativeBridge
from ..session import SessionReader
from . import siwc
from .storage import digest


class Unavailable(ValueError):
    pass


@dataclass
class Snapshot:
    identity: dict
    endpoint: str
    headers: dict = field(repr=False)
    auth: dict | None = field(default=None, repr=False)
    executable: str | None = None


def reference(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def siwc_path() -> Path:
    return codex_config.state_dir() / "channels" / "siwc-auth.json"


def resolve(route: dict) -> Snapshot:
    kind = route["kind"]
    identity = {"route": route["id"], "kind": kind, "credential_reference": digest(route), "identity_basis": "unknown"}
    if kind in {"codex-http", "codex-ws", "codex-cli", "bps"}:
        path = Path(route["auth_file"]) if route.get("auth_file") else codex_login.auth_path()
        reader = SessionReader(login="codex", codex_auth=path)
        bridge = NativeBridge(reader, lambda: None)
        try:
            headers = bridge.headers()
        except ValueError:
            raise Unavailable("codex_login_required") from None
        identity.update(account_ref=reference(headers["ChatGPT-Account-Id"]), identity_basis="selected_codex_account",
                        credential_reference=digest({"route": route, "path": str(path.resolve())}))
        endpoint = BASE_URL + "/responses"
        if kind == "codex-ws":
            endpoint = endpoint.replace("https:", "wss:", 1)
        if kind == "bps":
            from ..excel_upstream import RESPONSES_URL
            endpoint = RESPONSES_URL
            headers.update({"X-Basispoints-Auth-Mode": "chatgpt", "X-OpenAI-Account-Id": headers["ChatGPT-Account-Id"]})
        auth, executable = None, None
        if kind == "codex-cli":
            from ..cli import _find_codex
            executable = _find_codex(route.get("executable"))
            if not executable:
                raise Unavailable("codex_executable_missing")
            auth = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(auth.get("tokens", {}).get("id_token"), str):
                raise Unavailable("codex_id_token_missing")
            # Do not let an isolated CLI rotate the original login's refresh token.
            auth = {"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "tokens": {
                "access_token": headers["Authorization"].removeprefix("Bearer "), "id_token": auth["tokens"]["id_token"],
                "account_id": headers["ChatGPT-Account-Id"], "refresh_token": ""}}
            identity["executable"] = str(Path(executable).resolve())
        return Snapshot(identity, endpoint, headers, auth, executable)
    if kind == "siwc":
        path = Path(route.get("auth_file") or siwc_path())
        try:
            record = siwc.read_record(path)
        except (OSError, ValueError, TypeError):
            raise Unavailable("siwc_login_required") from None
        if siwc.DIRECT_SCOPE not in record.get("scopes", []):
            raise Unavailable("siwc_direct_permission_missing")
        if not isinstance(record.get("expires_at"), (int, float)) or record["expires_at"] <= time.time():
            raise Unavailable("siwc_login_expired")
        token = record.get("access_token")
        if not isinstance(token, str) or not token:
            raise Unavailable("siwc_token_missing")
        # A subject is not a workspace. Do not equate this grant with a Codex workspace.
        identity.update(account_ref=reference(record["subject"] + ":" + record["client_id"]),
                        identity_basis="validated_siwc_registration", credential_reference=digest({"route": route, "path": str(path.resolve())}))
        return Snapshot(identity, siwc.RESOURCE + "/responses", {"Authorization": "Bearer " + token})
    if kind == "responses-http":
        if "key_env" in route:
            key = os.environ.get(route["key_env"], "").strip()
        else:
            try:
                key = Path(route["key_file"]).read_text(encoding="utf-8").strip()
            except OSError:
                raise Unavailable("api_key_file_unavailable") from None
        if not key or any(c.isspace() for c in key):
            raise Unavailable("api_key_missing_or_invalid")
        identity.update(account_ref=reference(key), identity_basis="api_key_only_account_unverified")
        return Snapshot(identity, route["base_url"] + "/responses", {"Authorization": "Bearer " + key})
    raise Unavailable("unsupported_route")

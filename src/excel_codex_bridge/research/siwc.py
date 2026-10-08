"""Explicit SIWC login with PKCE/OIDC validation and a separate protected record.

Protocol: https://developers.openai.com/siwc/token-sharing-open-source/sign-in
No ordinary study starts a login, refreshes a token or changes billing paths.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import uuid
import webbrowser

import httpx
import jwt

from .storage import exclusive, private_directory, write_json

ISSUER = "https://auth.openai.com"
RESOURCE = "https://api.openai.com/v1"
DIRECT_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = "openid profile email offline_access resource.invoke " + DIRECT_SCOPE


def read_record(path: Path) -> dict:
    if path.is_symlink() or path.stat().st_size > 1024 * 1024:
        raise ValueError("Invalid SIWC credential file")
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise ValueError("SIWC credential file must be owner-only (chmod 600)")
    value = json.loads(path.read_text(encoding="utf-8"))
    if "dpapi" in value:
        if os.name != "nt":
            raise ValueError("This credential record belongs to a Windows user")
        from ..excel_upstream import _unprotect_windows_data
        value = json.loads(_unprotect_windows_data(base64.b64decode(value["dpapi"], validate=True)))
    if not isinstance(value, dict) or value.get("issuer") != ISSUER or not value.get("subject"):
        raise ValueError("Invalid SIWC identity record")
    return value


def save_record(path: Path, value: dict):
    if os.name == "nt":
        from ..excel_upstream import _protect_windows_data
        value = {"dpapi": base64.b64encode(_protect_windows_data(json.dumps(value).encode())).decode()}
    write_json(path, value)


def validate_token(tokens: dict, client_id: str, nonce: str, jwks: dict, previous=None) -> dict:
    try:
        header = jwt.get_unverified_header(tokens["id_token"])
        if header.get("alg") != "RS256":
            raise ValueError()
        keys = [key for key in jwks["keys"] if key.get("kid") == header.get("kid") and key.get("kty") == "RSA"
                and key.get("use", "sig") == "sig" and key.get("alg", "RS256") == "RS256"]
        if len(keys) != 1:
            raise ValueError()
        claims = jwt.decode(tokens["id_token"], jwt.PyJWK.from_dict(keys[0]).key, algorithms=["RS256"],
                            audience=client_id, issuer=ISSUER, options={"require": ["exp", "iat", "iss", "aud", "sub", "nonce"]})
        if not isinstance(claims["sub"], str) or not claims["sub"] or not hmac.compare_digest(claims["nonce"], nonce):
            raise ValueError()
        if claims.get("azp", client_id) != client_id:
            raise ValueError()
        if isinstance(claims["aud"], list) and len(claims["aud"]) > 1 and claims.get("azp") != client_id:
            raise ValueError()
        if previous and (claims["sub"] != previous["subject"] or client_id != previous["client_id"]):
            raise ValueError()
        if not isinstance(tokens.get("access_token"), str) or not tokens["access_token"] or tokens.get("token_type", "").lower() != "bearer":
            raise ValueError()
        seconds = tokens["expires_in"]
        if type(seconds) not in (int, float) or not 0 < seconds <= 90 * 86400:
            raise ValueError()
        scopes = tokens["scope"].split()
    except (jwt.PyJWTError, ValueError, TypeError, KeyError, AttributeError):
        raise ValueError("SIWC token signature, identity, nonce or grant validation failed") from None
    return {"issuer": ISSUER, "subject": claims["sub"], "client_id": client_id, "id_token": tokens["id_token"],
            "access_token": tokens["access_token"], "refresh_token": tokens.get("refresh_token"),
            "scopes": scopes, "expires_at": time.time() + seconds, "validated_at": time.time()}


def callback(values: dict, state: str, previous=None) -> tuple[str, str]:
    if any(len(value) != 1 for value in values.values()) or not hmac.compare_digest(values.get("state", [""])[0], state):
        raise ValueError("Invalid OAuth callback state")
    if "error" in values:
        raise ValueError("ChatGPT sign-in was declined or failed")
    client = values.get("client_id", [previous["client_id"] if previous else ""])[0]
    if not re.fullmatch(r"oaiapp_[A-Za-z0-9_-]{1,256}", client) or (previous and client != previous["client_id"]):
        raise ValueError("The callback did not return the expected issued client ID")
    code = values.get("code", [""])[0]
    if not code or len(code) > 8192:
        raise ValueError("Missing authorization code")
    return code, client


def login(path: Path, *, browser=True, timeout=180, client_factory=httpx.Client, opener=webbrowser.open):
    private_directory(path.parent)
    with exclusive(path.with_suffix(".lock")):
        previous = read_record(path) if path.exists() else None
        host_path = path.parent / "siwc-host.json"
        if not host_path.exists():
            write_json(host_path, {"id": "urn:uuid:" + str(uuid.uuid4())}, new=True)
        host = json.loads(host_path.read_text())["id"]
        state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        received = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass  # Authorization codes and id_token_hint must never enter logs.
            def do_GET(self):
                valid_host = self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"
                parsed = urlsplit(self.path)
                if not valid_host or self.headers.get("Origin") or parsed.path != "/auth/callback" or len(self.path) > 16384:
                    self.send_error(400)
                    return
                try:
                    values = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=20)
                except ValueError:
                    self.send_error(400)
                    return
                try:
                    received.append(callback(values, state, previous))
                    message = b"Sign-in callback received. Return to Codex Channels to see the verified result."
                    status = 200
                except ValueError as exc:
                    # Invalid states do not consume a valid pending login.
                    if values.get("state") == [state]:
                        received.append(exc)
                    message, status = b"Sign-in callback rejected.", 400
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(message)
        class LoopbackServer(HTTPServer):
            def get_request(self):
                connection, address = super().get_request()
                connection.settimeout(2)
                return connection, address
        with LoopbackServer(("127.0.0.1", 0), Handler) as server:
            server.timeout = 0.5
            redirect = f"http://127.0.0.1:{server.server_port}/auth/callback"
            params = {"client_id": previous["client_id"] if previous else "dynamic_agent_client", "ext_agent_host_id": host,
                      "response_type": "code", "redirect_uri": redirect, "scope": SCOPES, "resource": RESOURCE,
                      "state": state, "nonce": nonce, "code_challenge_method": "S256",
                      "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")}
            if not previous:
                params["agent_name_hint"] = "Codex Channels"
            elif browser:
                params["id_token_hint"] = previous["id_token"]
            if previous and DIRECT_SCOPE not in previous.get("scopes", []):
                params["prompt"] = "consent"
            url = ISSUER + "/api/accounts/authorize?" + urlencode(params)
            print("Continue with ChatGPT — authorize Codex Channels in your browser.", flush=True)
            if browser:
                if not opener(url):
                    raise ValueError("Could not open a browser; retry login-siwc with --no-browser")
            else:
                print(url, flush=True)  # No ID-token hint in a printed URL.
            deadline = time.monotonic() + timeout
            while not received and time.monotonic() < deadline:
                server.handle_request()
        if not received:
            raise ValueError("Sign-in timed out; no credentials were replaced")
        if isinstance(received[0], Exception):
            raise received[0]
        code, issued = received[0]
        with client_factory(follow_redirects=False, timeout=30, proxy=os.environ.get("EXCEL_BRIDGE_PROXY") or None) as client:
            response = client.post(ISSUER + "/api/accounts/oauth/token", data={"grant_type": "authorization_code",
                "client_id": issued, "code": code, "code_verifier": verifier, "redirect_uri": redirect, "resource": RESOURCE})
            if response.status_code != 200:
                raise ValueError(f"SIWC token exchange returned HTTP {response.status_code}; no retry was attempted")
            metadata = client.get(ISSUER + "/.well-known/openid-configuration")
            if metadata.status_code != 200:
                raise ValueError("OpenAI identity metadata is unavailable")
            identity = metadata.json()
            jwks_url = identity.get("jwks_uri", "")
            parts = urlsplit(jwks_url)
            if identity.get("issuer") != ISSUER or parts.scheme != "https" or parts.netloc != "auth.openai.com" or parts.query or parts.fragment:
                raise ValueError("Unexpected OpenAI identity metadata")
            keys = client.get(jwks_url)
            if keys.status_code != 200:
                raise ValueError("OpenAI signing keys are unavailable")
            record = validate_token(response.json(), issued, nonce, keys.json(), previous)
        record["ext_agent_host_id"] = host
        save_record(path, record)
        return {"signed_in": True, "direct_inference_enabled": DIRECT_SCOPE in record["scopes"], "credential_file": str(path)}

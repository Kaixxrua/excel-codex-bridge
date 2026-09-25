"""Opt-in, single-account Excel upstream for a private SUB2API network.

The ordinary local bridge is deliberately unchanged. This sidecar requires
separate data/control-plane keys; session administration additionally requires
a real loopback peer (use docker exec over SSH, never a forwarded address).
"""

from __future__ import annotations

import contextlib
import hmac
import io
import ipaddress
import json
import os
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import excel_upstream, images, sse
from .server import MAX_BODY_BYTES, Bridge, build_upstream_client
from .session import SessionReader

SESSION_PATH = "/admin/session"
MAX_SESSION_BYTES = 64 * 1024
API_PATHS = {"/v1/models", "/models", "/v1/responses", "/responses"}


def read_secret(name: str) -> str:
    """Read an environment variable or Docker secret, without echoing either."""
    value, filename = os.environ.get(name), os.environ.get(name + "_FILE")
    if value is not None and filename is not None:
        raise ValueError(f"Set only {name} or {name}_FILE, not both")
    if filename is not None:
        try:
            with Path(filename).open("r", encoding="utf-8") as handle:
                value = handle.read(258).strip()
        except (OSError, UnicodeError):
            raise ValueError(f"Cannot read {name}_FILE") from None
    if not isinstance(value, str) or not 32 <= len(value) <= 256 or any(
        ord(char) < 33 or ord(char) > 126 for char in value
    ):
        raise ValueError(f"{name} must contain 32-256 printable non-space ASCII characters")
    return value


@dataclass(frozen=True)
class GatewayKeys:
    api: str = field(repr=False)
    admin: str = field(repr=False)

    def __post_init__(self):
        for key in (self.api, self.admin):
            if not isinstance(key, str) or not 32 <= len(key) <= 256 or any(
                ord(char) < 33 or ord(char) > 126 for char in key
            ):
                raise ValueError("Gateway keys must be 32-256 printable non-space ASCII characters")
        if hmac.compare_digest(self.api, self.admin):
            raise ValueError("API and admin keys must be different")

    @classmethod
    def from_env(cls):
        return cls(read_secret("EXCEL_SUB2API_API_KEY"), read_secret("EXCEL_SUB2API_ADMIN_KEY"))


class PushedSessionReader(SessionReader):
    """Never scan a server filesystem for a user's Office credentials."""

    @property
    def method(self):
        return "ssh-import-memory-only"

    def refresh(self, *, force: bool = False):
        return self.status()


def session_summary(reader: SessionReader) -> dict:
    status = reader.status()
    return {key: status[key] for key in (
        "configured", "expired", "expires_at", "expires_in_seconds"
    )}


class GatewayGuard:
    def __init__(self, app, keys: GatewayKeys):
        self.app, self.keys = app, keys

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 1008})
        if scope["type"] != "http":
            return
        headers = scope.get("headers", [])
        path = scope["path"]
        error = None
        if any(k.lower() == b"origin" for k, _ in headers):
            error = sse.openai_error_response(403, "Browser-originated requests are not accepted")
        elif path == "/healthz" and scope["method"] == "GET":
            pass
        elif path in API_PATHS or path == SESSION_PATH:
            admin = path == SESSION_PATH
            peer = (scope.get("client") or ("", 0))[0]
            try:
                local = ipaddress.ip_address(peer).is_loopback
            except ValueError:
                local = False
            if admin and not local:
                error = sse.openai_error_response(404, "Not found")
            else:
                values = [v for k, v in headers if k.lower() == b"authorization"]
                parts = values[0].split(b" ", 1) if len(values) == 1 else []
                key = self.keys.admin if admin else self.keys.api
                if (len(parts) != 2 or parts[0].lower() != b"bearer"
                        or not hmac.compare_digest(parts[1], key.encode("ascii"))):
                    error = sse.openai_error_response(
                        401, "Invalid API key", headers={"www-authenticate": "Bearer"}
                    )
        else:
            error = sse.openai_error_response(404, "Not found")
        if error is not None:
            return await error(scope, receive, send)
        await self.app(scope, receive, send)


class BodyTooLarge(ValueError):
    pass


def decode_json(raw: bytes, encoding: str, limit: int) -> dict:
    """Bound decompressed data too, before parsing an authenticated request."""
    if len(raw) > limit:
        raise BodyTooLarge
    encoding = encoding.strip().lower()
    if not encoding:
        encoding = "gzip" if raw.startswith(b"\x1f\x8b") else (
            "zstd" if raw.startswith(b"\x28\xb5\x2f\xfd") else "identity"
        )
    if encoding in {"gzip", "deflate"}:
        decoder = zlib.decompressobj(31 if encoding == "gzip" else zlib.MAX_WBITS)
        raw = decoder.decompress(raw, limit + 1)
        if len(raw) > limit or decoder.unconsumed_tail:
            raise BodyTooLarge
        if not decoder.eof or decoder.unused_data:
            raise ValueError("Invalid compressed body")
    elif encoding == "zstd":
        import zstandard
        try:
            with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw)) as reader:
                raw = reader.read(limit + 1)
        except zstandard.ZstdError:
            raise ValueError("Invalid compressed body") from None
    elif encoding != "identity":
        raise ValueError("Unsupported content encoding")
    if len(raw) > limit:
        raise BodyTooLarge
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")
    return payload


async def read_json(request: Request, limit: int) -> dict:
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > limit:
            raise BodyTooLarge
        raw.extend(chunk)
    return decode_json(bytes(raw), request.headers.get("content-encoding", ""), limit)


def create_app(keys: GatewayKeys, *, client_factory=build_upstream_client):
    reader = PushedSessionReader()
    bridge = Bridge(reader, client_factory)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            await bridge.aclose()
            reader.store.clear()

    app = FastAPI(title="excel-sub2api", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.bridge = bridge

    @app.get("/healthz")
    async def healthz():
        # Liveness only; session status belongs to the protected control plane.
        return {"ok": True}

    @app.get("/v1/models")
    @app.get("/models")
    async def models():
        return {"object": "list", "data": [
            {"id": model, "object": "model", "created": 0, "owned_by": "openai-excel"}
            for model in excel_upstream.MODEL_IDS
        ]}

    @app.post("/v1/responses")
    @app.post("/responses")
    async def responses(request: Request):
        try:
            body = await read_json(request, MAX_BODY_BYTES)
        except BodyTooLarge:
            return sse.openai_error_response(413, "Request body is too large")
        except (ValueError, zlib.error, RecursionError):
            return sse.openai_error_response(400, "Invalid request body")
        if "stream" in body and not isinstance(body["stream"], bool):
            return sse.openai_error_response(400, "stream must be a boolean")
        return await bridge.responses(body)

    @app.get(SESSION_PATH)
    async def get_session():
        return session_summary(reader)

    @app.post(SESSION_PATH)
    async def set_session(request: Request):
        try:
            body = await read_json(request, MAX_SESSION_BYTES)
            headers = body.get("headers")
            if not isinstance(headers, dict) or any(
                not isinstance(k, str) or not isinstance(v, str)
                or any(c in k + v for c in "\r\n\x00")
                for k, v in headers.items()
            ):
                raise ValueError("Invalid session headers")
            reader.store.configure(headers, tools_version_id=body.get("tools_version_id"),
                                   persist=False, allow_expired=False)
        except BodyTooLarge:
            return sse.openai_error_response(413, "Session payload is too large")
        except (ValueError, zlib.error, RecursionError):
            # Do not echo validation input, token, account ID, or raw body.
            return sse.openai_error_response(400, "Invalid or expired Excel session")
        bridge.pictures = images.Pictures()
        return JSONResponse(session_summary(reader))

    @app.delete(SESSION_PATH)
    async def clear_session():
        reader.store.clear()
        bridge.pictures = images.Pictures()
        return session_summary(reader)

    return GatewayGuard(app, keys)

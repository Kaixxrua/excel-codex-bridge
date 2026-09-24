"""A small image host for the bridge's pictures.

The Excel backend only fetches images from https URLs; it rejects the inline
``data:`` URLs Codex sends. The bridge therefore uploads each picture here and
passes the resulting URL on. Run this on a server the internet (OpenAI) can
reach, behind an https reverse proxy:

    IMAGE_HOST_PUBLIC_URL=https://img.example.com IMAGE_HOST_TOKENS=<secret> \\
        python -m excel_codex_bridge.image_host

Uploads need one of the tokens. File names are an HMAC of the content, so they
cannot be guessed and the same picture keeps the same URL (which keeps the
upstream prompt cache warm). Files are deleted ``IMAGE_HOST_TTL_HOURS`` after
their last upload.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from .local_host import MEDIA_TYPES, sniff

logger = logging.getLogger("excel_codex_bridge.image_host")

_NAME = re.compile(r"^[0-9a-f]{32}\.(png|jpg|gif|webp)$")
SERVE_HEADERS = {
    "cache-control": "private, max-age=300",
    "x-content-type-options": "nosniff",
    "x-robots-tag": "noindex, nofollow",
    "content-security-policy": "default-src 'none'",
}


@dataclass
class Settings:
    public_url: str
    tokens: tuple[str, ...]
    directory: Path
    ttl_seconds: float = 24 * 3600
    max_bytes: int = 10 * 1024 * 1024
    max_total_bytes: int = 1024 * 1024 * 1024

    @classmethod
    def from_env(cls) -> Settings:
        public_url = os.environ.get("IMAGE_HOST_PUBLIC_URL", "").strip().rstrip("/")
        tokens = tuple(t.strip() for t in os.environ.get("IMAGE_HOST_TOKENS", "").split(",") if t.strip())
        if not public_url.startswith(("https://", "http://")):
            raise SystemExit("Set IMAGE_HOST_PUBLIC_URL to the https address this host is reached at.")
        if not tokens:
            raise SystemExit("Set IMAGE_HOST_TOKENS to one or more comma-separated upload tokens.")
        if any(len(t) < 16 for t in tokens):
            raise SystemExit("Upload tokens must be at least 16 characters.")
        return cls(
            public_url=public_url,
            tokens=tokens,
            directory=Path(os.environ.get("IMAGE_HOST_DIR", "image-host-data")),
            ttl_seconds=float(os.environ.get("IMAGE_HOST_TTL_HOURS", "24")) * 3600,
            max_bytes=int(float(os.environ.get("IMAGE_HOST_MAX_MB", "10")) * 1024 * 1024),
            max_total_bytes=int(float(os.environ.get("IMAGE_HOST_MAX_TOTAL_MB", "1024")) * 1024 * 1024),
        )


class ImageStore:
    def __init__(self, settings: Settings, clock=time.time) -> None:
        self.settings = settings
        self.clock = clock
        self.images = settings.directory / "images"
        self.images.mkdir(parents=True, exist_ok=True)
        self.key = self._load_key(settings.directory / "name-key")

    @staticmethod
    def _load_key(path: Path) -> bytes:
        """A persistent secret, so names survive restarts but cannot be guessed."""
        try:
            key = path.read_bytes()
            if len(key) >= 32:
                return key
        except FileNotFoundError:
            pass
        key = secrets.token_bytes(32)
        path.write_bytes(key)
        with contextlib.suppress(OSError):
            path.chmod(0o600)
        return key

    def authorized(self, header: str | None) -> bool:
        if not header or not header.lower().startswith("bearer "):
            return False
        offered = header[7:].strip().encode()
        return any(hmac.compare_digest(offered, token.encode()) for token in self.settings.tokens)

    def put(self, data: bytes, ext: str) -> str:
        name = f"{hmac.new(self.key, data, hashlib.sha256).hexdigest()[:32]}.{ext}"
        path = self.images / name
        now = self.clock()
        if path.exists():
            os.utime(path, (now, now))
        else:
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.utime(tmp, (now, now))
            tmp.replace(path)
            self.enforce_total()
        return name

    def path(self, name: str) -> Path | None:
        if not _NAME.match(name):
            return None
        path = self.images / name
        try:
            if self.clock() - path.stat().st_mtime > self.settings.ttl_seconds:
                return None
        except FileNotFoundError:
            return None
        return path

    def _files(self) -> list[tuple[float, int, Path]]:
        files = []
        for path in self.images.iterdir():
            with contextlib.suppress(FileNotFoundError):
                stat = path.stat()
                files.append((stat.st_mtime, stat.st_size, path))
        return sorted(files)

    def expire(self) -> int:
        removed = 0
        cutoff = self.clock() - self.settings.ttl_seconds
        for mtime, _size, path in self._files():
            if mtime < cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def enforce_total(self) -> None:
        """Drop the oldest files once the store grows past its cap."""
        files = self._files()
        total = sum(size for _mtime, size, _path in files)
        for _mtime, size, path in files:
            if total <= self.settings.max_total_bytes:
                break
            path.unlink(missing_ok=True)
            total -= size


def create_app(settings: Settings, *, clock=time.time, sweep_every: float = 600.0) -> FastAPI:
    store = ImageStore(settings, clock)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async def sweep():
            while True:
                removed = await asyncio.to_thread(store.expire)
                if removed:
                    logger.info("expired %d image(s)", removed)
                await asyncio.sleep(sweep_every)

        task = asyncio.create_task(sweep())
        try:
            yield
        finally:
            task.cancel()

    app = FastAPI(title="excel-codex image host", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store = store

    def error(status: int, message: str) -> JSONResponse:
        return JSONResponse({"error": {"message": message}}, status_code=status)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.post("/upload")
    async def upload(request: Request):
        if not store.authorized(request.headers.get("authorization")):
            return error(401, "missing or wrong upload token")
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > settings.max_bytes:
            return error(413, f"images are limited to {settings.max_bytes} bytes")
        data = bytearray()
        async for chunk in request.stream():
            data += chunk
            if len(data) > settings.max_bytes:
                return error(413, f"images are limited to {settings.max_bytes} bytes")
        ext = sniff(bytes(data))
        if ext is None:
            return error(415, "only PNG, JPEG, GIF and WebP images are accepted")
        name = await asyncio.to_thread(store.put, bytes(data), ext)
        logger.info("stored %s (%d bytes)", name, len(data))
        return {"url": f"{settings.public_url}/i/{name}", "expires_in": int(settings.ttl_seconds)}

    @app.get("/i/{name}")
    async def image(name: str):
        path = store.path(name)
        if path is None:
            return error(404, "not found")
        return FileResponse(path, media_type=MEDIA_TYPES[name.rsplit(".", 1)[1]], headers=SERVE_HEADERS)

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    host, _, port = os.environ.get("IMAGE_HOST_LISTEN", "0.0.0.0:8080").rpartition(":")
    uvicorn.run(
        create_app(settings),
        host=host or "0.0.0.0",
        port=int(port),
        log_level="warning",
        log_config=None,
        proxy_headers=True,
        timeout_graceful_shutdown=3,
    )


if __name__ == "__main__":
    main()

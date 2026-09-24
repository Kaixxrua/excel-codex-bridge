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

``IMAGE_HOST_OPEN=1`` runs it as a public relay instead (``excel-codex
image-host relay``): anyone may upload, so each client address gets a quota
(``IMAGE_HOST_UPLOADS_PER_HOUR``, ``IMAGE_HOST_UPLOAD_MB_PER_DAY``), every
picture is decoded and saved afresh (no metadata or hidden bytes survive; needs
Pillow), and pictures are only served to the user agents in
``IMAGE_HOST_FETCHERS`` (OpenAI's by default), so the relay is useless as a
general image host. Behind a reverse proxy, set uvicorn's
``FORWARDED_ALLOW_IPS`` to the proxy's address so quotas see real clients.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import io
import logging
import os
import re
import secrets
import threading
import time
from collections import deque
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
# What OpenAI fetches pictures as ("OpenAI File Downloader").
DEFAULT_FETCHERS = ("OpenAI",)
MAX_PIXELS = 25_000_000


def _flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name, "").strip().lower()
    return default if not value else value in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    public_url: str
    tokens: tuple[str, ...]
    directory: Path
    ttl_seconds: float = 24 * 3600
    max_bytes: int = 10 * 1024 * 1024
    max_total_bytes: int = 1024 * 1024 * 1024
    # A public relay: uploads need no token.
    open_uploads: bool = False
    # Per client address; 0 is no limit.
    uploads_per_hour: int = 0
    upload_bytes_per_day: int = 0
    # User-agent substrings that may fetch pictures; empty lets anyone.
    fetchers: tuple[str, ...] = ()
    # Decode each picture and save it afresh (needs Pillow).
    reencode: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        public_url = os.environ.get("IMAGE_HOST_PUBLIC_URL", "").strip().rstrip("/")
        tokens = tuple(t.strip() for t in os.environ.get("IMAGE_HOST_TOKENS", "").split(",") if t.strip())
        open_uploads = _flag("IMAGE_HOST_OPEN")
        if not public_url.startswith(("https://", "http://")):
            raise SystemExit("Set IMAGE_HOST_PUBLIC_URL to the https address this host is reached at.")
        if not tokens and not open_uploads:
            raise SystemExit("Set IMAGE_HOST_TOKENS to one or more comma-separated upload tokens "
                             "(or IMAGE_HOST_OPEN=1 for a public relay).")
        if any(len(t) < 16 for t in tokens):
            raise SystemExit("Upload tokens must be at least 16 characters.")
        fetchers = os.environ.get("IMAGE_HOST_FETCHERS")
        if fetchers is None:
            fetchers = ",".join(DEFAULT_FETCHERS) if open_uploads else ""
        fetchers = "" if fetchers.strip() == "*" else fetchers
        settings = cls(
            public_url=public_url,
            tokens=tokens,
            directory=Path(os.environ.get("IMAGE_HOST_DIR", "image-host-data")),
            ttl_seconds=float(os.environ.get("IMAGE_HOST_TTL_HOURS", "1" if open_uploads else "24")) * 3600,
            max_bytes=int(float(os.environ.get("IMAGE_HOST_MAX_MB", "5" if open_uploads else "10")) * 1024 * 1024),
            max_total_bytes=int(float(os.environ.get("IMAGE_HOST_MAX_TOTAL_MB", "1024")) * 1024 * 1024),
            open_uploads=open_uploads,
            uploads_per_hour=int(os.environ.get("IMAGE_HOST_UPLOADS_PER_HOUR", "240" if open_uploads else "0")),
            upload_bytes_per_day=int(
                float(os.environ.get("IMAGE_HOST_UPLOAD_MB_PER_DAY", "300" if open_uploads else "0")) * 1024 * 1024
            ),
            fetchers=tuple(f.strip() for f in fetchers.split(",") if f.strip()),
            reencode=_flag("IMAGE_HOST_REENCODE", default=open_uploads),
        )
        if settings.reencode:
            try:
                import PIL  # noqa: F401
            except ImportError:
                raise SystemExit("Re-encoding pictures needs Pillow: pip install 'excel-codex-bridge[host]'.") from None
        return settings


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


class Quota:
    """Uploads per client address in the last hour, and bytes in the last day."""

    def __init__(self, settings: Settings, clock=time.time) -> None:
        self.settings = settings
        self.clock = clock
        self._seen: dict[str, deque[tuple[float, int]]] = {}

    def _recent(self, client: str) -> deque[tuple[float, int]]:
        uploads = self._seen.get(client, deque())
        cutoff = self.clock() - 86400
        while uploads and uploads[0][0] <= cutoff:
            uploads.popleft()
        return uploads

    def refusal(self, client: str, size: int = 0) -> str | None:
        """Why this client may not upload ``size`` more bytes now, or None."""
        uploads = self._recent(client)
        hour_ago = self.clock() - 3600
        per_hour = self.settings.uploads_per_hour
        if per_hour and sum(1 for when, _size in uploads if when > hour_ago) >= per_hour:
            return f"more than {per_hour} uploads in an hour from this address; try again later"
        per_day = self.settings.upload_bytes_per_day
        if per_day and sum(size for _when, size in uploads) + size > per_day:
            return f"more than {per_day // (1024 * 1024)} MB in a day from this address; try again tomorrow"
        return None

    def record(self, client: str, size: int) -> None:
        self._seen.setdefault(client, deque()).append((self.clock(), size))

    def forget_idle(self) -> None:
        for client in list(self._seen):
            if not self._recent(client):
                del self._seen[client]


class Unreadable(ValueError):
    pass


_PNG_MODES = {"1", "L", "LA", "I", "I;16", "P", "RGB", "RGBA"}


def reencode(data: bytes) -> tuple[bytes, str]:
    """The picture's pixels saved afresh: no metadata, no trailing bytes, one frame.

    JPEGs stay JPEGs (turned upright first, since the orientation tag goes);
    everything else becomes a PNG.
    """
    from PIL import Image, ImageOps

    try:
        with Image.open(io.BytesIO(data)) as source:
            if source.format not in {"PNG", "JPEG", "GIF", "WEBP"}:
                raise Unreadable(f"{source.format} pictures are not accepted")
            if source.width * source.height > MAX_PIXELS:
                raise Unreadable(f"pictures are limited to {MAX_PIXELS // 1_000_000} megapixels")
            source.seek(0)
            source.load()
            is_jpeg = source.format == "JPEG"
            picture = ImageOps.exif_transpose(source) if is_jpeg else source.copy()
    except Unreadable:
        raise
    except Exception as exc:  # Pillow raises many kinds for broken files
        raise Unreadable("the picture could not be read") from exc
    kept = {key: picture.info[key] for key in ("icc_profile", "transparency") if key in picture.info}
    picture.info = kept
    out = io.BytesIO()
    if is_jpeg:
        if picture.mode not in {"L", "RGB", "CMYK"}:
            picture = picture.convert("RGB")
        picture.save(out, "JPEG", quality=95, icc_profile=kept.get("icc_profile"))
        return out.getvalue(), "jpg"
    if picture.mode not in _PNG_MODES:
        picture = picture.convert("RGBA")
    picture.save(out, "PNG", icc_profile=kept.get("icc_profile"))
    return out.getvalue(), "png"


def create_app(settings: Settings, *, clock=time.time, sweep_every: float = 300.0) -> FastAPI:
    store = ImageStore(settings, clock)
    quota = Quota(settings, clock)
    quota_lock = threading.Lock()
    # Decoding is memory-hungry: a few at a time.
    decoding = asyncio.Semaphore(2)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async def sweep():
            while True:
                removed = await asyncio.to_thread(store.expire)
                if removed:
                    logger.info("expired %d image(s)", removed)
                with quota_lock:
                    quota.forget_idle()
                await asyncio.sleep(sweep_every)

        task = asyncio.create_task(sweep())
        try:
            yield
        finally:
            task.cancel()

    app = FastAPI(title="excel-codex image host", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store = store
    app.state.quota = quota

    def error(status: int, message: str) -> JSONResponse:
        return JSONResponse({"error": {"message": message}}, status_code=status)

    def client_of(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.post("/upload")
    async def upload(request: Request):
        if not settings.open_uploads and not store.authorized(request.headers.get("authorization")):
            return error(401, "missing or wrong upload token")
        client = client_of(request)
        with quota_lock:
            refusal = quota.refusal(client)
        if refusal:
            return error(429, refusal)
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > settings.max_bytes:
            return error(413, f"images are limited to {settings.max_bytes} bytes")
        data = bytearray()
        async for chunk in request.stream():
            data += chunk
            if len(data) > settings.max_bytes:
                return error(413, f"images are limited to {settings.max_bytes} bytes")
        data = bytes(data)
        ext = sniff(data)
        if ext is None:
            return error(415, "only PNG, JPEG, GIF and WebP images are accepted")
        with quota_lock:
            refusal = quota.refusal(client, len(data))
            if not refusal:
                quota.record(client, len(data))
        if refusal:
            return error(429, refusal)
        if settings.reencode:
            async with decoding:
                try:
                    data, ext = await asyncio.to_thread(reencode, data)
                except Unreadable as exc:
                    return error(415, str(exc))
        name = await asyncio.to_thread(store.put, data, ext)
        logger.info("stored %s (%d bytes)", name, len(data))
        return {"url": f"{settings.public_url}/i/{name}", "expires_in": int(settings.ttl_seconds)}

    @app.get("/i/{name}")
    async def image(name: str, request: Request):
        if settings.fetchers:
            agent = request.headers.get("user-agent", "").lower()
            if not any(fetcher.lower() in agent for fetcher in settings.fetchers):
                return error(403, "pictures here are only for the model that was sent them")
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

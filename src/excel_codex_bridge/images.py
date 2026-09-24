"""Pictures for a backend that only fetches them by link.

Codex sends pictures inline, as ``data:`` URLs; the Excel backend rejects
those and only takes links that OpenAI can fetch.  The bridge swaps each
picture for such a link, in one of three modes (``excel-codex image-host``):

- ``local`` (default): the picture stays in this process's memory and is
  served through a temporary Cloudflare link (see ``local_host``);
- ``remote``: it is uploaded to an image host you run (see ``image_host``);
- ``off``: the catalog tells Codex the models take text only.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import codex_config, local_host

log = logging.getLogger("excel_codex_bridge.images")

ENV_HOST = "EXCEL_BRIDGE_IMAGE_HOST"
ENV_TOKEN = "EXCEL_BRIDGE_IMAGE_TOKEN"
CONFIG_NAME = "image-host.json"
LOCAL, REMOTE, OFF = "local", "remote", "off"
DEFAULT_EXPIRES_IN = 24 * 3600
CACHE_SIZE = 256


@dataclass(frozen=True)
class Setting:
    mode: str
    url: str = ""
    token: str = ""
    source: str = "default"

    @property
    def upload_url(self) -> str:
        return f"{self.url}/upload"


DEFAULT_SETTING = Setting(LOCAL)


def config_path(directory: Path | None = None) -> Path:
    return (directory or codex_config.state_dir()) / CONFIG_NAME


def clean_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url.endswith("/upload"):
        url = url[: -len("/upload")]
    return url


def _parse(value: str, token: str, source: str) -> Setting:
    if value.lower() in {OFF, "0", "none", "false"}:
        return Setting(OFF, source=source)
    if value.lower() == LOCAL:
        return Setting(LOCAL, source=source)
    return Setting(REMOTE, clean_url(value), token.strip(), source=source)


def load_setting(directory: Path | None = None) -> Setting:
    value = os.environ.get(ENV_HOST, "").strip()
    if value:
        return _parse(value, os.environ.get(ENV_TOKEN, ""), ENV_HOST)
    path = config_path(directory)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return DEFAULT_SETTING
    if not isinstance(data, dict):
        return DEFAULT_SETTING
    token = data.get("token") if isinstance(data.get("token"), str) else ""
    mode = data.get("mode")
    if mode in {LOCAL, OFF}:
        return Setting(mode, source=str(path))
    if isinstance(data.get("url"), str) and data["url"].strip():
        return Setting(REMOTE, clean_url(data["url"]), token.strip(), source=str(path))
    return DEFAULT_SETTING


def save_setting(setting: Setting, directory: Path | None = None) -> Path:
    path = config_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"mode": setting.mode}
    if setting.mode == REMOTE:
        data.update(url=clean_url(setting.url), token=setting.token.strip())
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def cloudflared_path(directory: Path | None = None) -> str | None:
    return local_host.find_cloudflared(directory or codex_config.state_dir())


def pictures_available(setting: Setting, directory: Path | None = None) -> bool:
    if setting.mode == LOCAL:
        return cloudflared_path(directory) is not None
    return setting.mode == REMOTE


def check(setting: Setting, client: httpx.Client | None = None) -> tuple[bool, str]:
    """Is a remote host up, and is the token accepted?  Stores nothing."""
    own = client is None
    client = client or httpx.Client(timeout=20.0, **_proxy_kwargs())
    try:
        # The token is checked before the body, and an empty body is never an image.
        response = client.post(setting.upload_url, content=b"", headers=_auth(setting))
    except httpx.HTTPError as exc:
        return False, f"cannot reach {setting.upload_url}: {type(exc).__name__}: {exc}"
    finally:
        if own:
            client.close()
    if response.status_code == 415:
        return True, f"{setting.url} is up and accepts the token."
    if response.status_code == 401:
        return False, f"{setting.url} rejected the token."
    return False, f"{setting.upload_url} answered HTTP {response.status_code}; is this an excel-codex image host?"


def _auth(setting: Setting) -> dict[str, str]:
    return {"authorization": f"Bearer {setting.token}"} if setting.token else {}


def _proxy_kwargs() -> dict:
    # The same route the bridge uses for OpenAI.
    proxy = os.environ.get("EXCEL_BRIDGE_PROXY", "").strip() or None
    return {"proxy": proxy, "trust_env": proxy is None}


def build_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=20.0), **_proxy_kwargs())


def _decode_data_url(url: str) -> tuple[str, bytes] | None:
    header, sep, payload = url.partition(",")
    if not sep or ";base64" not in header:
        return None
    media_type = header[len("data:"):].split(";", 1)[0] or "application/octet-stream"
    try:
        return media_type, base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError):
        return None


class UploadError(Exception):
    pass


class HostUnavailable(UploadError):
    """The host cannot be reached at all: the request's other pictures are not tried."""


class RemoteHost:
    """Uploads to an ``image_host`` server; each picture once per half its lifetime."""

    def __init__(self, setting: Setting, client_factory=build_client, clock=time.monotonic) -> None:
        self.setting = setting
        self._client_factory = client_factory
        self._client: httpx.AsyncClient | None = None
        self._clock = clock
        # sha256 of the picture -> (url, time after which it is uploaded again)
        self._cache: OrderedDict[str, tuple[str, float]] = OrderedDict()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def link(self, media_type: str, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        now = self._clock()
        cached = self._cache.get(digest)
        if cached and now < cached[1]:
            self._cache.move_to_end(digest)
            return cached[0]
        url, expires_in = await self._upload(media_type, data)
        # Upload again well before the host deletes it; the URL stays the same.
        self._cache[digest] = (url, now + max(expires_in, 60) / 2)
        self._cache.move_to_end(digest)
        while len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)
        return url

    async def _upload(self, media_type: str, data: bytes) -> tuple[str, float]:
        if self._client is None:
            self._client = self._client_factory()
        try:
            response = await self._client.post(
                self.setting.upload_url,
                content=data,
                headers={**_auth(self.setting), "content-type": media_type},
            )
        except httpx.TransportError as exc:
            raise HostUnavailable(f"{type(exc).__name__}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise UploadError(f"{type(exc).__name__}: {exc}") from exc
        if response.status_code != 200:
            try:
                message = response.json()["error"]["message"]
            except (ValueError, KeyError, TypeError):
                message = response.text[:200]
            raise UploadError(f"HTTP {response.status_code}: {message}")
        try:
            payload = response.json()
            url = payload["url"]
        except (ValueError, KeyError, TypeError) as exc:
            raise UploadError("the image host sent no URL") from exc
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            raise UploadError("the image host sent no URL")
        expires_in = payload.get("expires_in")
        if not isinstance(expires_in, (int, float)) or expires_in <= 0:
            expires_in = DEFAULT_EXPIRES_IN
        log.info("uploaded a %d KB picture to the image host", max(1, len(data) // 1024))
        return url, float(expires_in)


class LocalHost:
    """Pictures served from this computer (``local_host``)."""

    def __init__(self, pictures: local_host.LocalPictures) -> None:
        self.pictures = pictures

    async def aclose(self) -> None:
        await asyncio.to_thread(self.pictures.stop)

    def settling(self) -> float:
        return self.pictures.settling()

    async def link(self, media_type: str, data: bytes) -> str:
        try:
            return await asyncio.to_thread(self.pictures.link, data)
        except ConnectionError as exc:
            raise HostUnavailable(str(exc)) from exc
        except ValueError as exc:
            raise UploadError(str(exc)) from exc


def _omitted(reason: str) -> dict:
    return {"type": "input_text", "text": f"[image content omitted: {reason}]"}


FETCH_FAILED = "OpenAI could not fetch it from its link"


class ImageUploader:
    """Swaps inline pictures in a Responses body for links."""

    def __init__(self, host=None, off_reason: str = "the bridge passes on text only") -> None:
        self.host = host
        self.off_reason = off_reason

    async def aclose(self) -> None:
        if self.host is not None:
            await self.host.aclose()

    def retry_delay(self) -> float:
        """Seconds to wait before sending refused picture links once more (0: do not)."""
        settling = getattr(self.host, "settling", None)
        return settling() if settling else 0.0

    async def rewrite(self, body: dict, *, links: bool = True) -> tuple[dict, int]:
        """The body with every inline picture replaced, and how many became links."""
        items = body.get("input")
        if not isinstance(items, list) or "data:" not in json.dumps(items):
            return body, 0
        state = _Rewrite(links)
        rewritten = [await self._walk(item, state) for item in items]
        return {**body, "input": rewritten}, state.linked

    async def _walk(self, value, state: _Rewrite):
        if isinstance(value, list):
            return [await self._walk(item, state) for item in value]
        if not isinstance(value, dict):
            return value
        url = value.get("image_url")
        if value.get("type") == "input_image" and isinstance(url, str) and url.startswith("data:"):
            return await self._replace(value, url, state)
        return {key: await self._walk(item, state) for key, item in value.items()}

    async def _replace(self, part: dict, data_url: str, state: _Rewrite) -> dict:
        if self.host is None:
            return _omitted(self.off_reason)
        if not state.links:
            return _omitted(FETCH_FAILED)
        if state.down is not None:
            return _omitted(state.down)
        decoded = _decode_data_url(data_url)
        if decoded is None:
            return _omitted("the picture could not be decoded")
        media_type, data = decoded
        try:
            url = await self.host.link(media_type, data)
        except UploadError as exc:
            log.warning("could not pass a picture on: %s", exc)
            reason = f"it could not be passed on ({exc})"
            if isinstance(exc, HostUnavailable):
                state.down = reason
            return _omitted(reason)
        state.linked += 1
        return {**part, "image_url": url}


@dataclass
class _Rewrite:
    links: bool
    linked: int = 0
    down: str | None = None


def build_uploader(setting: Setting | None = None, directory: Path | None = None) -> ImageUploader:
    directory = directory or codex_config.state_dir()
    setting = setting or load_setting(directory)
    if setting.mode == REMOTE:
        return ImageUploader(RemoteHost(setting))
    if setting.mode == LOCAL:
        binary = cloudflared_path(directory)
        if binary is not None:
            return ImageUploader(LocalHost(local_host.LocalPictures(binary, directory / "cloudflared-home")))
        return ImageUploader(off_reason="cloudflared was not found; see `excel-codex image-host`")
    return ImageUploader(off_reason="pictures are turned off; see `excel-codex image-host`")


def describe(setting: Setting, directory: Path | None = None) -> str:
    """One line for the bridge window."""
    if setting.mode == REMOTE:
        return f"Pictures: uploaded to {setting.url}, where they can be opened by their link for about a day."
    if setting.mode == OFF:
        return "Pictures: off (text only); `excel-codex image-host local` turns them on."
    if cloudflared_path(directory) is None:
        return "Pictures: off, because cloudflared was not found; `excel-codex image-host` explains."
    return ("Pictures: kept in memory on this computer; OpenAI fetches each one through a temporary "
            "Cloudflare link, for a few minutes after you send it.")

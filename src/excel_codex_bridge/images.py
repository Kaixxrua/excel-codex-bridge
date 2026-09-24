"""Pictures for the Excel backend.

Codex sends pictures inline, as ``data:`` URLs.  Where the backend takes them
that way (the add-in's own tools put pictures inline in their results) they
are passed on untouched.  Where it refuses them (it does in user messages),
each picture is uploaded the way the add-in's "Upload file" button does it:
``POST .../basispoints/api/attachments`` on the same session, and the request
then names it by the file id that comes back.  Either way the pictures go to
OpenAI only, like the rest of the request.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
from collections import OrderedDict
from dataclasses import dataclass, field

import httpx

from . import excel_upstream

log = logging.getLogger("excel_codex_bridge.images")

ATTACHMENTS_URL = excel_upstream.RESPONSES_URL.rsplit("/", 1)[0] + "/attachments"
UPLOAD_TIMEOUT = httpx.Timeout(120.0, connect=30.0)
CACHE_SIZE = 256
_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
_UPLOAD_DROPS = {"accept", "content-type", "content-length"}

DESCRIPTION = ("Pictures: sent to OpenAI with the request; when the Excel backend will not take one "
               "inline, it is uploaded to OpenAI the way the add-in's Upload file button does it.")


class UploadError(Exception):
    pass


class UploadUnavailable(UploadError):
    """The upload could not be made at all: the request's other pictures are not tried."""


def _decode_data_url(url: str) -> tuple[str, bytes] | None:
    header, sep, payload = url.partition(",")
    if not sep or ";base64" not in header:
        return None
    media_type = header[len("data:"):].split(";", 1)[0] or "application/octet-stream"
    try:
        return media_type, base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError):
        return None


def _omitted(reason: str) -> dict:
    return {"type": "input_text", "text": f"[image content omitted: {reason}]"}


def _error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text.strip()[:200] or "no details"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"][:200]
        if isinstance(payload.get("detail"), str):
            return payload["detail"][:200]
    return json.dumps(payload)[:200]


@dataclass
class Sent:
    """One request body, and how its pictures went into it."""

    body: dict
    # The kinds of item (``message``, ``function_call_output``...) whose pictures went inline.
    inline: set[str] = field(default_factory=set)
    # File ids the body names: all of them, those uploaded for it, and those uploaded long before.
    file_ids: set[str] = field(default_factory=set)
    uploaded: set[str] = field(default_factory=set)
    reused: set[str] = field(default_factory=set)

    @property
    def pictures(self) -> bool:
        return bool(self.inline or self.file_ids)


@dataclass
class _Walk:
    client: httpx.AsyncClient
    headers: dict
    kind: str
    omit: str | None
    sent: Sent
    # Uploads made for this request, on this attempt or an earlier one.
    fresh: set[str]
    down: str | None = None


class Pictures:
    """Puts the pictures of a Responses body the way the backend takes them."""

    def __init__(self) -> None:
        # Item kinds where the backend refused inline pictures (learnt, per process).
        self.refused: set[str] = set()
        # (account, sha256 of the picture) -> file id
        self._ids: OrderedDict[tuple[str, str], str] = OrderedDict()

    def refuse_inline(self, kind: str) -> None:
        self.refused.add(kind)

    def forget(self, file_ids: set[str]) -> None:
        for key in [key for key, file_id in self._ids.items() if file_id in file_ids]:
            del self._ids[key]

    async def rewrite(
        self, body: dict, client: httpx.AsyncClient, headers: dict, *,
        omit: str | None = None, fresh: set[str] = frozenset(),
    ) -> Sent:
        """``body`` with its pictures inline, uploaded, or (with ``omit``) left out.

        ``fresh``: file ids uploaded for this same request on an earlier attempt.
        """
        items = body.get("input")
        if not isinstance(items, list) or "data:" not in json.dumps(items):
            return Sent(body)
        sent = Sent(body)
        walk = _Walk(client, headers, "", omit, sent, set(fresh))
        rewritten = []
        for item in items:
            walk.kind = str(item.get("type") or "message") if isinstance(item, dict) else "message"
            rewritten.append(await self._walk(item, walk))
        sent.body = {**body, "input": rewritten}
        return sent

    async def _walk(self, value, walk: _Walk):
        if isinstance(value, list):
            return [await self._walk(item, walk) for item in value]
        if not isinstance(value, dict):
            return value
        url = value.get("image_url")
        if value.get("type") == "input_image" and isinstance(url, str) and url.startswith("data:"):
            return await self._picture(value, url, walk)
        return {key: await self._walk(item, walk) for key, item in value.items()}

    async def _picture(self, part: dict, data_url: str, walk: _Walk) -> dict:
        if walk.omit is not None:
            return _omitted(walk.omit)
        if walk.kind not in self.refused:
            walk.sent.inline.add(walk.kind)
            return part
        decoded = _decode_data_url(data_url)
        if decoded is None:
            return _omitted("the picture could not be decoded")
        media_type, data = decoded
        key = (walk.headers.get("chatgpt-account-id", ""), hashlib.sha256(data).hexdigest())
        file_id = self._ids.get(key)
        if file_id is not None:
            self._ids.move_to_end(key)
            if file_id not in walk.fresh:
                walk.sent.reused.add(file_id)
        elif walk.down is not None:
            return _omitted(walk.down)
        else:
            try:
                file_id = await upload(walk.client, walk.headers, media_type, data, key[1])
            except UploadError as exc:
                log.warning("could not upload a picture: %s", exc)
                reason = f"it could not be uploaded ({exc})"
                if isinstance(exc, UploadUnavailable):
                    walk.down = reason
                return _omitted(reason)
            self._ids[key] = file_id
            while len(self._ids) > CACHE_SIZE:
                self._ids.popitem(last=False)
            walk.sent.uploaded.add(file_id)
            walk.fresh.add(file_id)
        walk.sent.file_ids.add(file_id)
        picture = {name: item for name, item in part.items() if name != "image_url"}
        picture.setdefault("detail", "auto")
        return {**picture, "file_id": file_id}


async def upload(client: httpx.AsyncClient, headers: dict, media_type: str, data: bytes, digest: str) -> str:
    """Upload a picture as the add-in does; its OpenAI file id."""
    name = f"picture-{digest[:12]}.{_EXTENSIONS.get(media_type, 'png')}"
    upload_headers = {key: value for key, value in headers.items() if key.lower() not in _UPLOAD_DROPS}
    upload_headers["accept"] = "application/json"
    try:
        response = await client.post(
            ATTACHMENTS_URL,
            headers=upload_headers,
            files={"file": (name, data, media_type)},
            timeout=UPLOAD_TIMEOUT,
        )
    except httpx.TransportError as exc:
        raise UploadUnavailable(f"{type(exc).__name__}: {exc}") from exc
    except httpx.HTTPError as exc:
        raise UploadError(f"{type(exc).__name__}: {exc}") from exc
    if not response.is_success:
        raise UploadError(f"HTTP {response.status_code}: {_error_message(response)}")
    try:
        file_id = response.json().get("openai_file_id")
    except (ValueError, AttributeError):
        file_id = None
    if not isinstance(file_id, str) or not file_id.strip():
        raise UploadError("no file id came back")
    log.info("uploaded a %d KB picture", max(1, len(data) // 1024))
    return file_id.strip()

"""Explicit Codex HTTP transport, without the Excel protocol or inference retries.

Only the caller executes tools. Requests keep their native tools, instructions,
reasoning and continuation items; an unsuccessful stream is never completed on
the caller's behalf. Credentials go only to the fixed Codex origin.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import time
import uuid
from collections import OrderedDict

import httpx
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import __version__, sse
from .excel_stream import kept_alive

BASE_URL = "https://chatgpt.com/backend-api/codex"
CATALOG_NAME = "codex-native-model-catalog.json"
ROUTE = "codex"
CLIENT_VERSION = "0.156.1"
MAX_OUTPUT_BYTES = 32 * 1024 * 1024
log = logging.getLogger("excel_codex_bridge")


class RouteError(ValueError):
    def __init__(self, message: str, status: int = 400, code: str = "route_unavailable"):
        super().__init__(message)
        self.status, self.code = status, code


def prepare(body: dict) -> dict:
    """Validate HTTP continuation and preserve native request semantics."""
    if not isinstance(body.get("model"), str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", body["model"]):
        raise RouteError("A native Codex model name is required.")
    if body["model"].endswith("-excel"):
        raise RouteError("Excel model aliases belong to --route excel; select a model from the Codex catalog.")
    if "stream" in body and not isinstance(body["stream"], bool):
        raise RouteError("stream must be a boolean.")
    if body.get("store") not in (None, False):
        raise RouteError("The Codex route requires store:false.")
    if any(body.get(k) is not None for k in ("previous_response_id", "conversation")):
        raise RouteError("Send complete input history on the Codex HTTP route; server-side continuation is unavailable.")
    if not isinstance(body.get("input"), (str, list)):
        raise RouteError("input must contain the request and its history.")
    result = copy.deepcopy(body)
    if isinstance(result["input"], str):
        result["input"] = [{"role": "user", "content": result["input"]}]
    result.setdefault("instructions", "")
    result["store"], result["stream"] = False, True
    return result


class NativeBridge:
    def __init__(self, reader, client_factory):
        self.reader, self.client_factory = reader, client_factory
        self._client = None
        self._catalog = None
        self._catalog_key = None
        self._catalog_at = 0.0
        self._conversations: OrderedDict[str, str] = OrderedDict()

    @property
    def client(self):
        if self._client is None:
            self._client = self.client_factory()
        return self._client

    async def aclose(self):
        if self._client is not None:
            await self._client.aclose()

    def headers(self) -> dict:
        status = self.reader.refresh()
        if self.reader.source != "codex" or not status.get("configured") or status.get("expired"):
            raise RouteError("The Codex route needs a current Codex ChatGPT login. Run codex login; for a sidecar, push-session --login codex.", 401, "codex_login_required")
        try:
            stored = self.reader.store.request_headers(stream=True)
        except RuntimeError:
            raise RouteError("Codex credentials are unavailable; sign in again with codex login.", 401, "codex_login_required") from None
        return {"Authorization": stored["authorization"],
                "ChatGPT-Account-Id": stored["chatgpt-account-id"],
                "Accept": "text/event-stream", "Content-Type": "application/json",
                "User-Agent": f"excel-codex-bridge/{__version__}",
                "OpenAI-Beta": "responses=experimental"}

    @staticmethod
    def error(exc: RouteError) -> Response:
        return sse.openai_error_response(exc.status, str(exc), code=exc.code,
                                        headers={"X-Excel-Bridge-Route": ROUTE})

    @staticmethod
    async def rejected(upstream: httpx.Response) -> Response:
        await upstream.aread()
        status = upstream.status_code
        try:
            payload = upstream.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("error"), dict):
                raise ValueError()
        except ValueError:
            payload = {"error": {"type": "upstream_error", "code": "codex_http_error",
                                 "message": f"Codex returned HTTP {status}; this request was not retried or sent to another route."}}
        headers = {"X-Excel-Bridge-Route": ROUTE}
        if upstream.headers.get("retry-after"):
            headers["Retry-After"] = upstream.headers["retry-after"]
        # Never turn a redirect into a credential-forwarding follow-up request.
        return JSONResponse(payload, status_code=status if status >= 400 else 502, headers=headers)

    async def catalog(self) -> dict:
        headers = self.headers()
        key = hashlib.sha256((headers["Authorization"] + headers["ChatGPT-Account-Id"]).encode()).hexdigest()
        if self._catalog_key == key and self._catalog is not None and time.monotonic() - self._catalog_at < 300:
            return copy.deepcopy(self._catalog)
        try:
            response = await self.client.get(BASE_URL + "/models", headers=headers,
                                             params={"client_version": CLIENT_VERSION},
                                             follow_redirects=False, timeout=30)
        except httpx.RequestError:
            raise RouteError("Could not reach the Codex model catalog; check the network or proxy.", 502) from None
        if response.status_code != 200:
            raise RouteError(f"Codex model catalog returned HTTP {response.status_code}.",
                             response.status_code if response.status_code >= 400 else 502)
        try:
            catalog = response.json()
            models = catalog["models"]
            if not isinstance(models, list) or not models or any(
                not isinstance(m, dict) or not isinstance(m.get("slug"), str) for m in models
            ):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise RouteError("Codex returned an invalid model catalog.", 502) from None
        # The native catalog supplies its own context limits, prompts and tools.
        self._catalog, self._catalog_key, self._catalog_at = {"models": models}, key, time.monotonic()
        return copy.deepcopy(self._catalog)

    async def models(self):
        try:
            catalog = await self.catalog()
        except RouteError as exc:
            return self.error(exc)
        return {"object": "list", "data": [
            {"id": m["slug"], "object": "model", "created": 0, "owned_by": "openai-codex"}
            for m in catalog["models"] if m.get("visibility") != "hide"
        ]}

    async def images(self, operation, body):
        return self.error(RouteError("Standalone image generation is unavailable on this Codex HTTP adapter. Image inputs are forwarded unchanged.", 501, "unsupported_route_capability"))

    async def responses(self, body: dict) -> Response:
        try:
            payload, headers = prepare(body), self.headers()
            account = hashlib.sha256(headers["ChatGPT-Account-Id"].encode()).hexdigest()[:24]
            conversation = body.get("prompt_cache_key")
            if isinstance(conversation, str) and conversation:
                key = hashlib.sha256(conversation.encode()).hexdigest()
                if self._conversations.get(key, account) != account:
                    raise RouteError("This conversation used another account. Start a new conversation after switching accounts.", 409, "account_binding_changed")
                self._conversations[key] = account
                self._conversations.move_to_end(key)
                if len(self._conversations) > 4096:
                    self._conversations.popitem(last=False)
        except RouteError as exc:
            return self.error(exc)
        request_id, started = uuid.uuid4().hex, time.monotonic()
        log.info("route=codex request=%s model=%s status=submitted", request_id, payload["model"])
        try:
            request = self.client.build_request("POST", BASE_URL + "/responses", headers=headers, json=payload)
            upstream = await self.client.send(request, stream=True, follow_redirects=False)
        except httpx.RequestError as exc:
            status, message = sse.upstream_request_error_status_and_message(exc)
            log.warning("route=codex request=%s status=connection_failed", request_id)
            return self.error(RouteError(message + "; no retry or route fallback was attempted.", status))
        if upstream.status_code != 200:
            try:
                log.warning("route=codex request=%s http=%s", request_id, upstream.status_code)
                return await self.rejected(upstream)
            finally:
                await upstream.aclose()

        opening = {"id": "resp_" + request_id, "object": "response", "created_at": int(time.time()),
                   "status": "in_progress", "model": payload["model"], "output": []}
        response_headers = {"X-Excel-Bridge-Route": ROUTE, "X-Excel-Bridge-Request-Id": request_id,
                            "Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

        async def relay():
            terminal = "interrupted"
            chunks = kept_alive(upstream.aiter_bytes(), opening, backend="Codex")
            try:
                async for event, raw in sse.iter_sse_messages(chunks):
                    if raw == "[DONE]":
                        continue
                    try:
                        item = json.loads(raw)
                        kind = item.get("type", event)
                        if not isinstance(kind, str) or (event and event != kind):
                            raise ValueError("inconsistent event type")
                    except (ValueError, AttributeError):
                        terminal = "invalid_event"
                        yield sse.sse_encode("response.failed", {"type": "response.failed", "response": {
                            **opening, "status": "failed", "error": {"code": "invalid_upstream_event", "message": "Codex returned an invalid stream event."}}})
                        return
                    if kind in {"response.completed", "response.failed", "response.incomplete"}:
                        expected = kind.removeprefix("response.")
                        final = item.get("response")
                        if not isinstance(final, dict) or final.get("status") != expected:
                            terminal = "invalid_terminal"
                            yield sse.sse_encode("response.failed", {"type": "response.failed", "response": {
                                **opening, "status": "failed", "error": {"code": "invalid_terminal", "message": "Codex returned an inconsistent terminal event."}}})
                            return
                        terminal = expected
                    yield sse.sse_encode(event or kind or "message", item)
                    if terminal != "interrupted":
                        return
            finally:
                await chunks.aclose()
                await upstream.aclose()
                log.info("route=codex request=%s status=%s seconds=%.3f", request_id, terminal, time.monotonic() - started)

        if body.get("stream"):
            return StreamingResponse(relay(), media_type="text/event-stream", headers=response_headers)
        size, output_items, terminal = 0, {}, None
        events = relay()
        try:
            async for event, raw in sse.iter_sse_messages(events):
                size += len(raw.encode())
                if size > MAX_OUTPUT_BYTES:
                    return self.error(RouteError("Buffered response exceeded 32 MiB; use stream:true.", 502))
                item = json.loads(raw)
                kind = item.get("type", event)
                if kind == "response.output_item.done" and isinstance(item.get("item"), dict):
                    output_items[item.get("output_index", len(output_items))] = item["item"]
                if kind in {"response.completed", "response.failed", "response.incomplete"}:
                    terminal = item.get("response")
                    break
        finally:
            await events.aclose()
        if not isinstance(terminal, dict) or terminal.get("status") != "completed":
            return JSONResponse(terminal, status_code=502, headers=response_headers) if isinstance(terminal, dict) else self.error(RouteError("Codex ended without response.completed.", 502))
        if not terminal.get("output") and output_items:
            terminal["output"] = [output_items[i] for i in sorted(output_items)]
        return JSONResponse(terminal, headers=response_headers)

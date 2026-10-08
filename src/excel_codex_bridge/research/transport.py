"""Strict Responses collection shared by all research HTTP/WS adapters."""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass, field
import json
import re
import time

import httpx

from .. import excel_upstream, sse
from ..native_upstream import BASE_URL, CLIENT_VERSION
from ..ws_upstream import open_response
from .credentials import Unavailable

MAX_BYTES = 8 * 1024 * 1024


@dataclass
class Result:
    status: str
    output: list = field(default_factory=list, repr=False)
    answer: str = field(default="", repr=False)
    seconds: float = 0
    http_status: int | None = None
    model: str | None = None
    effort: str | None = None
    usage: dict = field(default_factory=dict)
    error_code: str | None = None
    error_param: str | None = None
    wire: str = "http"
    submissions_known: bool = True

    def record(self):
        return {key: value for key, value in vars(self).items() if key not in {"output", "answer"}}


def error_fields(value) -> dict:
    error = value.get("error", value) if isinstance(value, dict) else {}
    if not isinstance(error, dict):
        return {}
    fields = {}
    for source, target, pattern in (("code", "error_code", r"[a-z][a-z0-9_]{0,63}"),
                                    ("param", "error_param", r"[a-zA-Z][a-zA-Z0-9_.\[\]]{0,63}")):
        if isinstance(error.get(source), str) and re.fullmatch(pattern, error[source]):
            fields[target] = error[source]
    return fields


def wire_payload(kind: str, payload: dict) -> dict:
    body = copy.deepcopy(payload)
    if kind == "bps":
        if body.get("tools"):
            raise Unavailable("bps_research_text_only")
        original = body["model"]
        body["model"] = original + "-excel"
        body = excel_upstream.prepare_responses_body(body)
        if body.get("model") != original or body.get("reasoning_effort") != payload["reasoning"]["effort"]:
            raise Unavailable("bps_model_or_effort_mapping_mismatch")
    elif kind == "codex-ws":
        body.pop("stream", None)
        body["type"] = "response.create"
    return body


def terminal(response: dict, expected: dict, items: dict, wire: str) -> Result:
    status = response.get("status")
    if status not in {"completed", "failed", "incomplete"}:
        return Result("invalid_terminal", wire=wire)
    reported_model = response.get("model")
    reported_effort = (response.get("reasoning") or {}).get("effort")
    result = Result(status, model=reported_model if reported_model == expected["model"] else "mismatch" if reported_model else None,
                    effort=reported_effort if reported_effort == expected["reasoning"]["effort"] else "mismatch" if reported_effort else None, wire=wire)
    usage = response.get("usage")
    if isinstance(usage, dict):
        result.usage = {key: usage[key] for key in ("input_tokens", "output_tokens", "total_tokens") if type(usage.get(key)) is int and usage[key] >= 0}
    if status != "completed":
        code = (response.get("error") or {}).get("code")
        result.error_code = code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) else "upstream_failure"
        return result
    if result.model != expected["model"] or result.effort == "mismatch":
        result.status = "model_or_effort_mismatch"
        return result
    output = response.get("output") or [items[index] for index in sorted(items)]
    if not isinstance(output, list) or any(not isinstance(item, dict) for item in output):
        result.status = "invalid_output"
        return result
    result.output = output
    result.answer = "".join(part.get("text", "") for item in output if item.get("type") == "message"
                            for part in item.get("content", []) if part.get("type") == "output_text")
    return result


async def collect(upstream, payload: dict, wire="http") -> Result:
    if upstream.status_code != 200:
        raw = bytearray()
        async for chunk in upstream.aiter_bytes():
            raw.extend(chunk)
            if len(raw) > 65536:
                break
        try:
            details = error_fields(json.loads(raw)) if len(raw) <= 65536 else {}
        except ValueError:
            details = {}
        return Result("http_error", http_status=upstream.status_code, wire=wire, **details)
    size = 0
    async def chunks():
        nonlocal size
        async for chunk in upstream.aiter_bytes():
            size += len(chunk)
            if size > MAX_BYTES:
                raise ValueError("response_limit")
            yield chunk
    if "application/json" in upstream.headers.get("content-type", ""):
        raw = b"".join([chunk async for chunk in chunks()])
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("object") != "response":
            return Result("invalid_json_response", wire=wire)
        return terminal(value, payload, {}, "json")
    items = {}
    async for event, raw in sse.iter_sse_messages(chunks()):
        if raw == "[DONE]":
            continue
        item = json.loads(raw)
        kind = item.get("type", event)
        if not isinstance(kind, str) or (event and event != kind):
            return Result("invalid_event", wire=wire)
        if kind == "response.output_item.done":
            index = item.get("output_index")
            if type(index) is not int or not 0 <= index < 1024 or not isinstance(item.get("item"), dict):
                return Result("invalid_output", wire=wire)
            items[index] = item["item"]
        if kind in {"response.completed", "response.failed", "response.incomplete"}:
            response = item.get("response")
            if not isinstance(response, dict) or response.get("status") != kind.split(".")[1]:
                return Result("invalid_terminal", wire=wire)
            return terminal(response, payload, items, wire)
        if kind == "error":
            status = item.get("status")
            return Result("upstream_error", http_status=status if type(status) is int else None, wire=wire, **error_fields(item))
    return Result("missing_terminal", wire=wire)


class Transport:
    def __init__(self, client_factory=None, websocket_open=open_response, cli_call=None):
        if client_factory is None:
            from ..server import build_upstream_client
            client_factory = build_upstream_client
        self.client = client_factory()
        self.websocket_open, self.cli_call = websocket_open, cli_call

    async def aclose(self):
        await self.client.aclose()

    async def preflight(self, route, snapshot, config):
        kind = route["kind"]
        if kind not in {"codex-http", "codex-ws", "siwc"}:
            return
        url = BASE_URL + "/models" if kind.startswith("codex") else "https://api.openai.com/v1/models"
        params = {"client_version": CLIENT_VERSION} if kind.startswith("codex") else None
        async with self.client.stream("GET", url, headers=snapshot.headers, params=params, follow_redirects=False, timeout=30) as response:
            if response.status_code != 200:
                raise Unavailable("catalog_http_" + str(response.status_code))
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > MAX_BYTES:
                    raise Unavailable("catalog_too_large")
        models = json.loads(raw).get("models", [])
        model = next((model for model in models if model.get("slug") == config["model"]), None)
        if model is None:
            raise Unavailable("model_absent_from_account_catalog")
        efforts = {level["effort"] for level in model.get("supported_reasoning_levels", [])}
        if efforts and config["effort"] not in efforts:
            raise Unavailable("effort_absent_from_account_catalog")

    async def call(self, route, snapshot, payload, timeout) -> Result:
        started = time.monotonic()
        kind = route["kind"]
        async def once():
            if kind == "codex-cli":
                from .native_cli import call
                return await (self.cli_call or call)(snapshot, payload)
            if kind == "codex-ws":
                response = await self.websocket_open(payload, snapshot.headers)
            else:
                body = wire_payload(kind, payload)
                request = self.client.build_request("POST", snapshot.endpoint, headers={**snapshot.headers, "Accept": "text/event-stream", "Content-Type": "application/json"}, json=body)
                response = await self.client.send(request, stream=True, follow_redirects=False)
            try:
                result = await collect(response, payload, "websocket" if kind == "codex-ws" else "sse")
                if result.http_status is None:
                    result.http_status = response.status_code
                return result
            finally:
                await response.aclose()
        try:
            result = await asyncio.wait_for(once(), timeout)
        except (TimeoutError, asyncio.TimeoutError):
            result = Result("completion_unknown_timeout")
        except (httpx.RequestError, OSError):
            result = Result("connection_error")
        except (ValueError, TypeError, KeyError, AttributeError):
            result = Result("protocol_error")
        result.seconds = round(time.monotonic() - started, 3)
        # Only allowlisted metadata is persisted. Never store an upstream error body.
        for value in snapshot.headers.values():
            for field in ("error_code", "error_param"):
                if any(secret and secret in (getattr(result, field) or "") for secret in (value, value.removeprefix("Bearer "))):
                    setattr(result, field, "redacted")
        return result

"""Responses/SSE helpers.

Extracted from ghcp_proxy's ``format_translation.py`` and ``util.py``
(Unlicense); only the pieces the Excel path needs are kept, unchanged in
behaviour.
"""

from __future__ import annotations

import codecs
import json

import httpx
from fastapi.responses import JSONResponse


def _openai_error_type_for_status(status_code: int) -> str:
    if status_code == 400:
        return "invalid_request_error"
    if status_code == 401:
        return "authentication_error"
    if status_code == 403:
        return "permission_error"
    if status_code == 404:
        return "not_found_error"
    if status_code == 429:
        return "rate_limit_error"
    return "server_error"


def openai_error_response(
    status_code: int,
    message: str,
    error_type: str | None = None,
    code=None,
    param=None,
    headers: dict | None = None,
) -> JSONResponse:
    payload = {
        "error": {
            "message": message,
            "type": error_type or _openai_error_type_for_status(status_code),
            "param": param,
            "code": code,
        }
    }
    return JSONResponse(content=payload, status_code=status_code, headers=headers)


def upstream_request_error_status_and_message(exc: httpx.RequestError) -> tuple[int, str]:
    if isinstance(exc, httpx.TimeoutException):
        return 504, "Upstream request timed out"
    if isinstance(exc, httpx.ConnectError):
        return 502, "Upstream connection failed"
    return 502, "Upstream request failed"


# ─── SSE ──────────────────────────────────────────────────────────────────────

def sse_encode(event_name: str, payload: dict) -> bytes:
    return f"event: {event_name}\ndata: {json.dumps(payload, separators=(',', ':'), ensure_ascii=False)}\n\n".encode("utf-8")


def parse_sse_block(raw_block: str) -> tuple[str | None, str | None]:
    event_name = None
    data_lines = []
    for line in raw_block.replace("\r\n", "\n").split("\n"):
        if not line or line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if not data_lines:
        return event_name, None
    return event_name, "\n".join(data_lines)


async def iter_sse_messages(byte_iter):
    buffer = ""
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        async for chunk in byte_iter:
            if isinstance(chunk, bytes):
                buffer += decoder.decode(chunk)
            else:
                buffer += str(chunk)

            normalized = buffer.replace("\r\n", "\n")
            while "\n\n" in normalized:
                raw_block, normalized = normalized.split("\n\n", 1)
                event_name, data = parse_sse_block(raw_block)
                if data is not None:
                    yield event_name, data
            buffer = normalized
    except Exception:
        # The connection can break right after the last event, before the
        # blank line that ends it; that event is still whole. (A cut-off one
        # fails to parse as JSON and is dropped by the reader.)
        trailing = buffer.strip()
        if trailing:
            event_name, data = parse_sse_block(trailing)
            if data is not None:
                yield event_name, data
        raise

    buffer += decoder.decode(b"", final=True)

    trailing = buffer.strip()
    if trailing:
        event_name, data = parse_sse_block(trailing)
        if data is not None:
            yield event_name, data


def is_response_completed_event(event_name: str | None, data: str | None) -> bool:
    if str(event_name or "").strip().lower() == "response.completed":
        return True
    if not data or data == "[DONE]":
        return False
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and str(payload.get("type") or "").strip().lower() == "response.completed"


# ─── Reasoning presentation ───────────────────────────────────────────────────

_CODEX_THINKING_SUMMARY_HEADER = "**Thinking**\n\n"


def ensure_codex_reasoning_header(text: str) -> str:
    """Ensure reasoning summary text begins with a bold header for Codex."""
    if not isinstance(text, str) or not text.strip():
        return text
    stripped = text.lstrip()
    if stripped.startswith("**") or stripped.startswith("#"):
        return text
    return f"{_CODEX_THINKING_SUMMARY_HEADER}{text}"


def normalize_reasoning_item_for_client(item: dict, fallback_text: str = "") -> dict:
    """Normalize a Responses reasoning item so Codex displays it.

    Codex only renders reasoning items from the ``summary`` array and drops
    items whose ``summary`` is empty.
    """
    if not isinstance(item, dict) or item.get("type") != "reasoning":
        return item

    summary_parts = []
    raw_summary = item.get("summary")
    if isinstance(raw_summary, list):
        for p in raw_summary:
            if isinstance(p, dict) and isinstance(p.get("text"), str):
                summary_parts.append(p["text"])
            elif isinstance(p, str):
                summary_parts.append(p)
    summary_text = "".join(summary_parts)

    content_parts = []
    raw_content = item.get("content")
    if isinstance(raw_content, list):
        for p in raw_content:
            if isinstance(p, dict) and isinstance(p.get("text"), str):
                content_parts.append(p["text"])
            elif isinstance(p, str):
                content_parts.append(p)
    content_text = "".join(content_parts)

    text = summary_text or content_text or fallback_text
    if not text and item.get("encrypted_content"):
        text = "*Thinking process completed.*"

    if text:
        formatted = ensure_codex_reasoning_header(text)
        item["summary"] = [{"type": "summary_text", "text": formatted}]
        item["content"] = [{"type": "reasoning_text", "text": formatted}]

    return item


def normalize_response_reasoning_for_client(payload: dict) -> dict:
    """Normalize all reasoning items in a completed Responses payload."""
    if not isinstance(payload, dict):
        return payload
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if isinstance(item, dict) and item.get("type") == "reasoning":
                normalize_reasoning_item_for_client(item)
    return payload


# ─── Output text ──────────────────────────────────────────────────────────────

def extract_item_text(item) -> str:
    if not isinstance(item, dict):
        return ""

    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for entry in content:
            if not isinstance(entry, dict):
                continue
            if isinstance(entry.get("text"), str):
                parts.append(entry["text"])
            elif isinstance(entry.get("input_text"), str):
                parts.append(entry["input_text"])
        return "".join(parts)

    if isinstance(item.get("text"), str):
        return item["text"]
    if isinstance(item.get("input_text"), str):
        return item["input_text"]
    return ""


def extract_response_output_text(payload: dict) -> str | None:
    if not isinstance(payload, dict):
        return None

    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text

    output = payload.get("output")
    if not isinstance(output, list):
        return None

    parts = []
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message":
            continue
        if str(item.get("role", "")).lower() != "assistant":
            continue
        text = extract_item_text(item).strip()
        if text:
            parts.append(text)

    if not parts:
        return None
    return "\n\n".join(parts)

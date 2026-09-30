"""Streaming tool-call relay for the Excel upstream.

Extracted verbatim (apart from imports) from ghcp_proxy's ``proxy.py``
(Unlicense).  Basispoints rejects client-declared tools, so ``excel_upstream``
describes Codex's tools in the prompt and the model calls them through the
add-in's ``run_officejs`` function.  This transform holds back anything that
may turn out to be such a call and, at ``response.completed``, re-emits it as
the ``function_call`` / ``custom_tool_call`` Codex actually declared, one for
each call when the model makes several at once.

Two additions of this project's own keep Codex from sending a request again
when the answer already arrived or is still coming.  Codex takes a stream
without ``response.completed`` as broken and retries it with the finished
items in the history, and the model then tends to repeat them word for word,
so a stream the upstream cuts off after its last finished item is completed
here.  And Codex drops a stream it hears nothing from for five minutes, so
``kept_alive`` fills any silence towards it with ``response.in_progress``,
and ends a stream the backend gives up on with ``response.failed`` saying why.
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx

from . import excel_upstream
from . import sse as format_translation

log = logging.getLogger("excel_codex_bridge")

KEEPALIVE_SECONDS = 15.0
OPENING_EVENTS = {"response.created", "response.in_progress", "response.queued"}
_TERMINAL_EVENTS = {"response.completed", "response.failed", "response.incomplete"}
# An error code Codex does not know: it shows the message and tries again.
UPSTREAM_ERROR_CODE = "upstream_error"


async def _with_ticks(items, wait):
    """Yield from ``items``, and ``None`` each time nothing came within ``wait()`` seconds (``None``: no limit)."""
    iterator = items.__aiter__()
    pending = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(iterator.__anext__())
            limit = wait()
            done, _ = await asyncio.wait({pending}, timeout=None if limit is None else max(limit, 0.0))
            if not done:
                yield None
                continue
            task, pending = pending, None
            try:
                message = task.result()
            except StopAsyncIteration:
                return
            yield message
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.wait({pending})
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            await aclose()


def _event(block: bytes) -> tuple[str, dict | None, bool]:
    """(the event's type, its data, whether it is whole) for one SSE event's bytes."""
    name, data = format_translation.parse_sse_block(block.decode("utf-8", "replace"))
    if data is None:
        return str(name or "").strip().lower(), None, False
    if data == "[DONE]":
        return "", None, True
    try:
        payload = json.loads(data)
    except ValueError:
        return str(name or "").strip().lower(), None, False
    if not isinstance(payload, dict):
        return str(name or "").strip().lower(), None, True
    return str(name or payload.get("type") or "").strip().lower(), payload, True


def reported_error(payload: dict) -> dict | None:
    """The error an ``error`` event reports, as ``code`` and ``message``.

    Basispoints nests it (``{"type": "error", "error": {...}}``); the
    Responses API puts ``code`` and ``message`` beside ``type``.
    """
    error = payload.get("error") if isinstance(payload.get("error"), dict) else payload
    code, message = error.get("code"), error.get("message")
    if not code and not message:
        return None
    return {"code": str(code) if code else UPSTREAM_ERROR_CODE,
            "message": str(message) if message else f"The Excel backend reported {code}."}


def failed_event(response: dict, error: dict) -> bytes:
    failed = {**response, "status": "failed", "error": error}
    return format_translation.sse_encode("response.failed", {"type": "response.failed", "response": failed})


def _spent(seconds: float) -> str:
    return f"{seconds / 60:.0f} minutes" if seconds >= 120 else f"{seconds:.0f} s"


def _detail(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


async def kept_alive(chunks, opening: dict, every: float | None = None):
    """The SSE bytes of ``chunks`` event by event, with something for the client at least every ``every`` seconds.

    Codex drops a stream it hears nothing from for five minutes and sends the
    request again.  A stream goes quiet while the model thinks (a compaction,
    which declares no tools, too), while a tool call is held back until it is
    whole, and before the backend's first event; that silence gets
    ``response.in_progress``.  Nothing having opened the stream yet,
    ``opening`` is announced as ``response.created`` instead, and the
    backend's own opening events are then left out, so the client hears the
    stream open once.  After the last event, silence stays silence.

    A stream that stops before its last event (``response.completed``,
    ``.failed`` or ``.incomplete``), mostly the backend closing the
    connection mid-answer, ends with ``response.failed`` instead of just
    stopping: Codex then shows why when it sends the request again.  It
    carries what the backend reported in an ``error`` event, if it did, as
    Codex ignores those: a context window exceeded then reaches Codex as
    such, and it compacts the conversation.
    """
    every = KEEPALIVE_SECONDS if every is None else every
    loop = asyncio.get_running_loop()
    started = sent_at = loop.time()
    buffer = b""
    announced = finished = False
    beat: bytes | None = None  # response.in_progress, once the stream has opened
    told = opening  # the response the client was told of
    reported: dict | None = None  # the error the backend reported
    events, last = 0, ""

    def until_beat() -> float | None:
        return None if finished else sent_at + every - loop.time()

    def in_progress(response: dict) -> bytes:
        return format_translation.sse_encode(
            "response.in_progress", {"type": "response.in_progress", "response": response}
        )

    def passes(block: bytes) -> bool:
        """Take note of the event ``block``; False for one to leave out."""
        nonlocal beat, finished, told, reported, events, last
        kind, payload, _ = _event(block)
        response = payload.get("response") if payload is not None else None
        events += 1
        last = kind or last
        if kind in OPENING_EVENTS:
            if announced:
                return False
            told = response if isinstance(response, dict) else opening
            beat = in_progress(told)
        elif kind in _TERMINAL_EVENTS:
            finished = True
        elif kind == "error" and payload is not None:
            reported = reported_error(payload) or reported
            if reported is not None:
                log.warning("the Excel backend reported an error: %s: %s", reported["code"], reported["message"][:500])
        return True

    cut_off: Exception | None = None
    try:
        async for chunk in _with_ticks(chunks, until_beat):
            if chunk is None:
                if beat is None:
                    announced = True
                    beat = in_progress(opening)
                    yield format_translation.sse_encode(
                        "response.created", {"type": "response.created", "response": opening}
                    )
                else:
                    yield beat
                sent_at = loop.time()
                continue
            if buffer.endswith(b"\r") and chunk.startswith(b"\n"):
                buffer = buffer[:-1]
            start = max(len(buffer) - 1, 0)  # a blank line may begin at the old end
            buffer += chunk.replace(b"\r\n", b"\n")
            end = buffer.find(b"\n\n", start)
            while end != -1:
                block, buffer = buffer[: end + 2], buffer[end + 2:]
                end = buffer.find(b"\n\n")
                if passes(block):
                    yield block
                    sent_at = loop.time()
    except Exception as exc:  # noqa: BLE001 - handled below
        cut_off = exc
    # The connection can break right after an event, before the blank line
    # that ends it: that event is still whole.  A cut-off one is left out, as
    # the client could not read it either.
    if buffer.strip() and _event(buffer)[2]:
        block = buffer.rstrip(b"\n") + b"\n\n"
        if passes(block):
            yield block
    broke = isinstance(cut_off, httpx.TransportError)
    if not finished:
        spent = loop.time() - started
        if reported is not None:
            error = reported
        elif broke:
            error = {"code": UPSTREAM_ERROR_CODE, "message": (
                f"The Excel backend closed the connection {_spent(spent)} into the answer, "
                f"before finishing it ({_detail(cut_off)}).")}
        elif cut_off is not None:
            error = {"code": UPSTREAM_ERROR_CODE,
                     "message": f"The bridge failed while passing on the answer ({_detail(cut_off)})."}
        else:
            error = {"code": UPSTREAM_ERROR_CODE,
                     "message": f"The Excel backend ended the answer {_spent(spent)} in, before finishing it."}
        log.warning(
            "the answer stopped after %.1f s and %d events (the last %s) before it was finished%s; "
            "Codex is told it failed",
            spent, events, last or "none", f" ({_detail(cut_off)})" if cut_off is not None else "",
        )
        yield failed_event(told, error)
    elif broke:
        log.debug("the Excel backend closed the connection after the last event (%s)", _detail(cut_off))
    if cut_off is not None and not broke:
        raise cut_off


def _completed_event_bytes(response_payload: dict) -> bytes:
    return format_translation.sse_encode(
        "response.completed",
        {"type": "response.completed", "response": response_payload},
    )


def _tool_call_item_event_bytes(tool_call: dict, *, output_index: int) -> list[bytes]:
    item = dict(tool_call)
    item["status"] = "in_progress"
    if tool_call["type"] == excel_upstream.TOOL_SEARCH_CALL:
        # A tool_search call streams no arguments: Codex reads them from the finished item.
        return [
            format_translation.sse_encode(
                event,
                {"type": event, "output_index": output_index, "item": payload},
            )
            for event, payload in (
                ("response.output_item.added", item),
                ("response.output_item.done", {**tool_call, "status": "completed"}),
            )
        ]
    if tool_call["type"] == "function_call":
        item["arguments"] = ""
        value_key = "arguments"
        delta_event = "response.function_call_arguments.delta"
        done_event = "response.function_call_arguments.done"
    else:
        item["input"] = ""
        value_key = "input"
        delta_event = "response.custom_tool_call_input.delta"
        done_event = "response.custom_tool_call_input.done"
    return [
        format_translation.sse_encode(
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "output_index": output_index,
                "item": item,
            },
        ),
        format_translation.sse_encode(
            delta_event,
            {
                "type": delta_event,
                "output_index": output_index,
                "item_id": tool_call["id"],
                "delta": tool_call[value_key],
            },
        ),
        format_translation.sse_encode(
            done_event,
            {
                "type": done_event,
                "output_index": output_index,
                "item_id": tool_call["id"],
                value_key: tool_call[value_key],
            },
        ),
        format_translation.sse_encode(
            "response.output_item.done",
            {
                "type": "response.output_item.done",
                "output_index": output_index,
                "item": {**tool_call, "status": "completed"},
            },
        ),
    ]


def excel_tool_stream_transform(source_body: dict):
    allowed_tools = excel_upstream.client_tool_types(source_body)
    if not allowed_tools:
        return None
    marker_open = excel_upstream.TOOL_CALL_MARKER_OPEN

    def _marker_hold_length(text: str) -> int:
        """Length of the text suffix that could still become a marker open tag."""
        max_probe = min(len(marker_open) - 1, len(text))
        for probe in range(max_probe, 0, -1):
            if text.endswith(marker_open[:probe]):
                return probe
        return 0

    async def transform(byte_iter):
        full_text = ""
        emitted_upto = 0
        marker_mode = False
        held_events: list[bytes] = []
        delta_template: dict = {}
        done_seen = False
        started_response: dict | None = None
        items_added = 0
        finished_items: list[tuple[int, dict]] = []
        finished = False

        def flush_text() -> list[bytes]:
            nonlocal emitted_upto
            pending = full_text[emitted_upto:]
            if not pending:
                return []
            emitted_upto = len(full_text)
            return [
                format_translation.sse_encode(
                    "response.output_text.delta",
                    {**delta_template, "type": "response.output_text.delta", "delta": pending},
                )
            ]

        def finish(event_type: str, payload: dict, encoded: bytes) -> list[bytes]:
            nonlocal emitted_upto, marker_mode, finished
            finished = True
            chunks: list[bytes] = []
            response = payload.get("response")
            response = response if isinstance(response, dict) else None
            if response:
                format_translation.normalize_response_reasoning_for_client(response)
            tool_calls: list[dict] = []
            if event_type == "response.completed":
                completed_text = full_text or (
                    format_translation.extract_response_output_text(response)
                    if response
                    else ""
                )
                marker_call = excel_upstream.extract_client_tool_call(
                    completed_text or "",
                    allowed_tools,
                )
                if marker_call is not None:
                    tool_calls = [marker_call]
                else:
                    tool_calls = excel_upstream.extract_native_client_tool_calls(
                        response,
                        source_body,
                    )
            if tool_calls:
                held_events.clear()
                emitted_upto = len(full_text)
                response_payload = excel_upstream.response_payload_with_tool_calls(
                    response,
                    tool_calls,
                    model_id=excel_upstream.excel_model_id(source_body.get("model"))
                    or excel_upstream.MODEL_ID,
                )
                # Each call keeps its place in the output, after any reasoning
                # or commentary already streamed.
                positions = {
                    item.get("call_id"): index
                    for index, item in enumerate(response_payload["output"])
                    if isinstance(item, dict)
                }
                for tool_call in tool_calls:
                    chunks.extend(
                        _tool_call_item_event_bytes(
                            tool_call,
                            output_index=positions.get(tool_call["call_id"], 0),
                        )
                    )
                chunks.append(_completed_event_bytes(response_payload))
                return chunks
            # Not a tool call after all: release everything that was held
            # back so the client still receives the full assistant text.
            chunks.extend(flush_text())
            chunks.extend(held_events)
            held_events.clear()
            marker_mode = False
            chunks.append(encoded)
            return chunks

        def completion_from_finished_items() -> dict | None:
            """A response.completed for a stream cut off after its last item, if it looks finished.

            A commentary message is sent before tool calls, so a stream that
            ends on one was cut off between items and is left to Codex to retry.
            """
            if not finished_items or len(finished_items) < items_added:
                return None
            output = [item for _, item in sorted(finished_items, key=lambda entry: entry[0])]
            last = output[-1]
            if last.get("type") == "message" and last.get("phase") == "commentary":
                return None
            response = dict(started_response or {})
            response["status"] = "completed"
            response["output"] = output
            return {"type": "response.completed", "response": response}

        cut_off: httpx.TransportError | None = None
        try:
            async for event_name, data in format_translation.iter_sse_messages(byte_iter):
                if data == "[DONE]":
                    done_seen = True
                    continue
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                event_type = str(event_name or payload.get("type") or "").strip().lower()
                event_output_index = payload.get("output_index")
                encoded = format_translation.sse_encode(event_type or "message", payload)

                if event_type in {"response.created", "response.in_progress"}:
                    response = payload.get("response")
                    if isinstance(response, dict) and started_response is None:
                        started_response = dict(response)
                elif event_type == "response.output_item.added":
                    items_added += 1
                elif event_type == "response.output_item.done":
                    item = payload.get("item")
                    if isinstance(item, dict):
                        position = (
                            event_output_index
                            if isinstance(event_output_index, int)
                            else len(finished_items)
                        )
                        finished_items.append((position, item))

                if event_type == "response.output_text.delta":
                    delta = payload.get("delta")
                    if isinstance(delta, str):
                        full_text += delta
                    delta_template = {
                        key: payload[key]
                        for key in ("item_id", "output_index", "content_index")
                        if key in payload
                    }
                    if marker_mode:
                        continue
                    search_start = max(0, emitted_upto - len(marker_open) + 1)
                    marker_pos = full_text.find(marker_open, search_start)
                    if marker_pos != -1:
                        marker_mode = True
                        pending = full_text[emitted_upto:marker_pos]
                        emitted_upto = marker_pos
                        if pending:
                            yield format_translation.sse_encode(
                                "response.output_text.delta",
                                {
                                    **delta_template,
                                    "type": "response.output_text.delta",
                                    "delta": pending,
                                },
                            )
                        continue
                    boundary = len(full_text) - _marker_hold_length(full_text)
                    if boundary > emitted_upto:
                        pending = full_text[emitted_upto:boundary]
                        emitted_upto = boundary
                        yield format_translation.sse_encode(
                            "response.output_text.delta",
                            {
                                **delta_template,
                                "type": "response.output_text.delta",
                                "delta": pending,
                            },
                        )
                    continue

                if event_type == "response.output_text.done":
                    if marker_mode:
                        held_events.append(encoded)
                        continue
                    for chunk in flush_text():
                        yield chunk
                    yield encoded
                    continue

                # Native tool-call events must never reach Codex raw: their
                # arguments follow the upstream's server-tool schema, and Codex
                # executing the un-normalized call fails and provokes retry
                # loops. Hold them until the conversion decision at completion.
                if event_type in {
                    "response.function_call_arguments.delta",
                    "response.function_call_arguments.done",
                    "response.custom_tool_call_input.delta",
                    "response.custom_tool_call_input.done",
                }:
                    held_events.append(encoded)
                    continue

                if event_type in {"response.output_item.added", "response.output_item.done"}:
                    item = payload.get("item")
                    item_type = (
                        item.get("type") if isinstance(item, dict) else None
                    )
                    if item_type in {"function_call", "custom_tool_call"}:
                        held_events.append(encoded)
                        continue
                    if (
                        event_type == "response.output_item.done"
                        and marker_mode
                        and item_type == "message"
                    ):
                        held_events.append(encoded)
                        continue
                    if item_type == "reasoning":
                        format_translation.normalize_reasoning_item_for_client(item)
                        yield format_translation.sse_encode(event_type, payload)
                        continue
                    yield encoded
                    continue

                if event_type in _TERMINAL_EVENTS:
                    for chunk in finish(event_type, payload, encoded):
                        yield chunk
                    continue

                yield encoded
        except httpx.TransportError as exc:
            cut_off = exc

        if not finished:
            completion = completion_from_finished_items()
            if completion is not None:
                log.warning(
                    "upstream stream ended before response.completed (%s); "
                    "completing it from the finished items",
                    type(cut_off).__name__ if cut_off is not None else "closed",
                )
                encoded = format_translation.sse_encode("response.completed", completion)
                for chunk in finish("response.completed", completion, encoded):
                    yield chunk
        if cut_off is not None and not finished:
            raise cut_off

        for chunk in flush_text():
            yield chunk
        for held in held_events:
            yield held
        if done_seen:
            yield b"data: [DONE]\n\n"

    return transform

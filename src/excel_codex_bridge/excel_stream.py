"""Streaming tool-call relay for the Excel upstream.

Extracted verbatim (apart from imports) from ghcp_proxy's ``proxy.py``
(Unlicense).  Basispoints rejects client-declared tools, so ``excel_upstream``
describes Codex's tools in the prompt and the model calls them through the
add-in's ``run_officejs`` function.  This transform holds back anything that
may turn out to be such a call and, at ``response.completed``, re-emits it as
the ``function_call`` / ``custom_tool_call`` Codex actually declared, one for
each call when the model makes several at once.

Two additions of this project's own keep Codex from sending a request again
when the answer already arrived: Codex takes a stream without
``response.completed`` as broken and retries it with the finished items in
the history, and the model then tends to repeat them word for word. So a
stream the upstream cuts off after its last finished item is completed here,
and while the upstream is silent ``response.in_progress`` is repeated so that
Codex's five-minute idle timeout does not fire during long thinking.
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
_TERMINAL_EVENTS = {"response.completed", "response.failed", "response.incomplete"}


async def _with_ticks(messages, every: float):
    """Yield from ``messages``, and ``None`` each time nothing came for ``every`` seconds."""
    iterator = messages.__aiter__()
    pending = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(iterator.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=every)
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


def _completed_event_bytes(response_payload: dict) -> bytes:
    return format_translation.sse_encode(
        "response.completed",
        {"type": "response.completed", "response": response_payload},
    )


def _tool_call_item_event_bytes(tool_call: dict, *, output_index: int) -> list[bytes]:
    item = dict(tool_call)
    item["status"] = "in_progress"
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


def excel_tool_stream_transform(
    source_body: dict,
    *,
    keepalive_every: float = KEEPALIVE_SECONDS,
):
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
        keepalive: bytes | None = None
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
            async for message in _with_ticks(
                format_translation.iter_sse_messages(byte_iter), keepalive_every
            ):
                if message is None:
                    if keepalive is not None and not finished:
                        yield keepalive
                    continue
                event_name, data = message
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
                    if isinstance(response, dict):
                        if started_response is None:
                            started_response = dict(response)
                        if keepalive is None or event_type == "response.in_progress":
                            keepalive = format_translation.sse_encode(
                                "response.in_progress",
                                {"type": "response.in_progress", "response": response},
                            )
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

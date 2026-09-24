"""Streaming tool-call relay for the Excel upstream.

Extracted verbatim (apart from imports) from ghcp_proxy's ``proxy.py``
(Unlicense).  Basispoints rejects client-declared tools, so ``excel_upstream``
describes Codex's tools in the prompt and the model calls them through the
add-in's ``run_officejs`` function.  This transform holds back anything that
may turn out to be such a call and, at ``response.completed``, re-emits it as
the ``function_call`` / ``custom_tool_call`` Codex actually declared.
"""

from __future__ import annotations

import json

from . import excel_upstream
from . import sse as format_translation


def excel_tool_call_event_bytes(
    tool_call: dict,
    response_payload: dict,
    *,
    output_index: int,
) -> list[bytes]:
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
        format_translation.sse_encode(
            "response.completed",
            {
                "type": "response.completed",
                "response": response_payload,
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
        native_tool_output_index: int | None = None

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
                    if isinstance(event_output_index, int) and event_output_index >= 0:
                        native_tool_output_index = event_output_index
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

            if event_type in {"response.completed", "response.failed", "response.incomplete"}:
                response = payload.get("response")
                response = response if isinstance(response, dict) else None
                if response:
                    format_translation.normalize_response_reasoning_for_client(response)
                tool_call = None
                if event_type == "response.completed":
                    completed_text = full_text or (
                        format_translation.extract_response_output_text(response)
                        if response
                        else ""
                    )
                    tool_call = excel_upstream.extract_client_tool_call(
                        completed_text or "",
                        allowed_tools,
                    )
                    if tool_call is None:
                        tool_call = excel_upstream.extract_native_client_tool_call(
                            response,
                            source_body,
                        )
                if tool_call is not None:
                    held_events.clear()
                    emitted_upto = len(full_text)
                    response_payload = excel_upstream.response_payload_with_tool_call(
                        response,
                        tool_call,
                        model_id=excel_upstream.excel_model_id(source_body.get("model"))
                        or excel_upstream.MODEL_ID,
                    )
                    tool_output_index = (
                        native_tool_output_index
                        if native_tool_output_index is not None
                        else 0
                    )
                    for chunk in excel_tool_call_event_bytes(
                        tool_call,
                        response_payload,
                        output_index=tool_output_index,
                    ):
                        yield chunk
                    continue
                # Not a tool call after all: release everything that was held
                # back so the client still receives the full assistant text.
                for chunk in flush_text():
                    yield chunk
                for held in held_events:
                    yield held
                held_events.clear()
                marker_mode = False
                yield encoded
                continue

            yield encoded

        for chunk in flush_text():
            yield chunk
        for held in held_events:
            yield held
        if done_seen:
            yield b"data: [DONE]\n\n"

    return transform

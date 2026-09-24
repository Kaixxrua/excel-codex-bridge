"""Streams the upstream cuts off, and keepalives while it is silent.

Codex retries a stream that ends without response.completed, with the items
it already finished in the history, and the model then tends to repeat them
word for word: in the app every message shows up twice.
"""

from __future__ import annotations

import asyncio
import json
import unittest

import httpx

from excel_codex_bridge.excel_stream import excel_tool_stream_transform
from excel_codex_bridge.server import create_app
from excel_codex_bridge.sse import iter_sse_messages

from helpers import session_headers
from test_server import StaticReader

SOURCE_BODY = {
    "model": "gpt-5.6-sol-excel",
    "tools": [{"type": "function", "name": "shell_command", "parameters": {"type": "object"}}],
}


def sse(event: str, payload: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode()


def created(response_id: str = "resp_cut") -> bytes:
    return sse("response.created", {"type": "response.created",
                                     "response": {"id": response_id, "object": "response", "status": "in_progress"}})


def message(index: int, text: str, phase: str) -> list[bytes]:
    item = {"type": "message", "id": f"msg_{index}", "role": "assistant", "phase": phase,
            "status": "in_progress", "content": []}
    done = dict(item, status="completed", content=[{"type": "output_text", "text": text}])
    return [
        sse("response.output_item.added", {"type": "response.output_item.added", "output_index": index, "item": item}),
        sse("response.output_text.delta", {"type": "response.output_text.delta", "item_id": f"msg_{index}",
                                           "output_index": index, "content_index": 0, "delta": text}),
        sse("response.output_text.done", {"type": "response.output_text.done", "item_id": f"msg_{index}",
                                          "output_index": index, "content_index": 0, "text": text}),
        sse("response.output_item.done", {"type": "response.output_item.done", "output_index": index, "item": done}),
    ]


def officejs_call(index: int, command: str) -> list[bytes]:
    """A client tool call the way the add-in's model makes it: through run_officejs."""
    arguments = json.dumps({
        "summary": "Run a command",
        "extended_summary": "Run a command",
        "code": json.dumps({"name": "shell_command", "arguments": {"command": command}}),
        "destructive": False,
        "references": [],
    })
    item = {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "run_officejs",
            "status": "in_progress", "arguments": ""}
    done = dict(item, status="completed", arguments=arguments)
    return [
        sse("response.output_item.added", {"type": "response.output_item.added", "output_index": index, "item": item}),
        sse("response.function_call_arguments.delta", {"type": "response.function_call_arguments.delta",
                                                       "output_index": index, "item_id": "fc_1", "delta": arguments}),
        sse("response.function_call_arguments.done", {"type": "response.function_call_arguments.done",
                                                      "output_index": index, "item_id": "fc_1", "arguments": arguments}),
        sse("response.output_item.done", {"type": "response.output_item.done", "output_index": index, "item": done}),
    ]


def completed(output: list[dict]) -> bytes:
    return sse("response.completed", {"type": "response.completed", "response": {
        "id": "resp_cut", "status": "completed", "output": output,
        "usage": {"input_tokens": 5, "output_tokens": 7}}})


async def source(chunks: list[bytes], *, then: Exception | None = None, pause: float = 0.0):
    for chunk in chunks:
        if pause:
            await asyncio.sleep(pause)
        yield chunk
    if then is not None:
        raise then


def parse(raw: bytes) -> list[tuple[str, dict]]:
    events = []
    for block in raw.decode().split("\n\n"):
        if not block.strip():
            continue
        name, data = None, ""
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
        events.append((name or "", {} if data == "[DONE]" else json.loads(data)))
    return events


def run(chunks, *, then=None, pause=0.0, keepalive_every=15.0):
    """Output events, and the error the transform ended with (if any)."""
    transform = excel_tool_stream_transform(SOURCE_BODY, keepalive_every=keepalive_every)
    out: list[bytes] = []

    async def go():
        async for chunk in transform(source(chunks, then=then, pause=pause)):
            out.append(chunk)

    error = None
    try:
        asyncio.run(go())
    except Exception as exc:  # noqa: BLE001 - the test inspects it
        error = exc
    return parse(b"".join(out)), error


def cut() -> httpx.RemoteProtocolError:
    return httpx.RemoteProtocolError("peer closed connection without sending complete message body")


class CutOffStreamTests(unittest.TestCase):
    def test_final_answer_cut_off_before_completed_is_completed(self):
        events, error = run([created(), *message(0, "Done: pelican.svg", "final_answer")], then=cut())
        self.assertIsNone(error)
        names = [name for name, _ in events]
        self.assertEqual(names[-1], "response.completed")
        self.assertEqual(names.count("response.output_item.done"), 1)
        response = events[-1][1]["response"]
        self.assertEqual(response["id"], "resp_cut")
        self.assertEqual(response["status"], "completed")
        self.assertEqual([item["id"] for item in response["output"]], ["msg_0"])

    def test_tool_call_cut_off_before_completed_still_reaches_codex(self):
        chunks = [created(), *message(0, "I will list the files.", "commentary"), *officejs_call(1, "ls")]
        events, error = run(chunks, then=cut())
        self.assertIsNone(error)
        calls = [payload["item"] for name, payload in events
                 if name == "response.output_item.done" and payload["item"]["type"] == "function_call"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "shell_command")
        self.assertEqual(json.loads(calls[0]["arguments"]), {"command": "ls"})
        self.assertEqual(events[-1][0], "response.completed")
        self.assertNotIn("run_officejs", json.dumps(events))

    def test_stream_closed_cleanly_without_completed_is_completed(self):
        events, error = run([created(), *message(0, "Done.", "final_answer")])
        self.assertIsNone(error)
        self.assertEqual(events[-1][0], "response.completed")

    def test_cut_off_inside_an_item_is_left_to_codex_to_retry(self):
        chunks = [created(), *message(0, "Half of the answ", "final_answer")[:2]]
        events, error = run(chunks, then=cut())
        self.assertIsInstance(error, httpx.RemoteProtocolError)
        self.assertNotIn("response.completed", [name for name, _ in events])

    def test_cut_off_after_a_commentary_message_is_left_to_codex_to_retry(self):
        # A tool call was probably on its way; completing here would end the turn.
        events, error = run([created(), *message(0, "I will list the files.", "commentary")], then=cut())
        self.assertIsInstance(error, httpx.RemoteProtocolError)
        self.assertNotIn("response.completed", [name for name, _ in events])

    def test_last_event_without_its_blank_line_is_still_read(self):
        output = [{"type": "message", "id": "msg_0", "role": "assistant", "phase": "final_answer",
                   "content": [{"type": "output_text", "text": "Done."}]}]
        chunks = [created(), *message(0, "Done.", "final_answer"), completed(output).rstrip(b"\n") + b"\n"]
        events, error = run(chunks, then=cut())
        self.assertIsNone(error)
        self.assertEqual(events[-1][0], "response.completed")
        # The upstream's own event, not one made up here.
        self.assertEqual(events[-1][1]["response"]["usage"]["output_tokens"], 7)

    def test_complete_stream_is_unchanged(self):
        output = [{"type": "message", "id": "msg_0", "role": "assistant", "phase": "final_answer",
                   "content": [{"type": "output_text", "text": "Done."}]}]
        chunks = [created(), *message(0, "Done.", "final_answer"), completed(output), b"data: [DONE]\n\n"]
        events, error = run(chunks)
        self.assertIsNone(error)
        names = [name for name, _ in events]
        self.assertEqual(names.count("response.completed"), 1)
        self.assertEqual(events[-2][1]["response"]["usage"]["output_tokens"], 7)
        self.assertEqual(events[-1], ("", {}))


class TrailingBlockTests(unittest.TestCase):
    def test_trailing_event_is_yielded_before_the_error(self):
        async def go():
            seen = []
            try:
                async for message in iter_sse_messages(source([b"event: a\ndata: 1\n\nevent: b\ndata: 2\n"], then=cut())):
                    seen.append(message)
            except httpx.RemoteProtocolError:
                seen.append("error")
            return seen

        self.assertEqual(asyncio.run(go()), [("a", "1"), ("b", "2"), "error"])


class KeepaliveTests(unittest.TestCase):
    def test_silent_upstream_gets_in_progress_repeated(self):
        output = [{"type": "message", "id": "msg_0", "role": "assistant", "phase": "final_answer",
                   "content": [{"type": "output_text", "text": "Done."}]}]
        chunks = [created(), *message(0, "Done.", "final_answer"), completed(output)]
        events, error = run(chunks, pause=0.2, keepalive_every=0.05)
        self.assertIsNone(error)
        names = [name for name, _ in events]
        self.assertEqual(names[0], "response.created")
        self.assertGreater(names.count("response.in_progress"), len(chunks))
        self.assertEqual(names[-1], "response.completed")
        beat = next(payload for name, payload in events if name == "response.in_progress")
        self.assertEqual(beat["response"]["id"], "resp_cut")

    def test_no_keepalive_before_the_upstream_starts_or_after_it_finishes(self):
        output = [{"type": "message", "id": "msg_0", "role": "assistant", "content": []}]

        async def slow():
            await asyncio.sleep(0.2)
            yield created()
            yield completed(output)
            await asyncio.sleep(0.2)

        transform = excel_tool_stream_transform(SOURCE_BODY, keepalive_every=0.05)

        async def go():
            return [chunk async for chunk in transform(slow())]

        names = [name for name, _ in parse(b"".join(asyncio.run(go())))]
        self.assertEqual(names, ["response.created", "response.completed"])


class BrokenBody(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        raise cut()


class BridgeCutOffTests(unittest.TestCase):
    def test_codex_gets_response_completed_when_upstream_cuts_off(self):
        body = [created(), *message(0, "Done: pelican.svg", "final_answer")]

        def handler(_request):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=BrokenBody(body))

        reader = StaticReader()
        reader.store.configure(session_headers(None), persist=False, allow_expired=True)
        app = create_app(reader, client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))

        async def go():
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
                return await client.post("/v1/responses", json={**SOURCE_BODY, "input": "draw", "stream": True})

        response = asyncio.run(go())
        self.assertEqual(response.status_code, 200)
        events = parse(response.content)
        self.assertEqual(events[-1][0], "response.completed")
        self.assertEqual([name for name, _ in events].count("response.output_item.done"), 1)


if __name__ == "__main__":
    unittest.main()

"""Streams the upstream cuts off, and keepalives while it is silent.

Codex retries a stream that ends without response.completed, with the items
it already finished in the history, and the model then tends to repeat them
word for word: in the app every message shows up twice.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from unittest import mock

import httpx

from excel_codex_bridge import excel_stream
from excel_codex_bridge.excel_stream import excel_tool_stream_transform, kept_alive
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


def error_event(code: str, message: str) -> bytes:
    """The way Basispoints reports a failure first."""
    return sse("error", {"type": "error", "error": {"type": "invalid_request_error", "code": code,
                                                     "message": message, "param": None}, "sequence_number": 2})


def failed(code: str, message: str) -> bytes:
    return sse("response.failed", {"type": "response.failed", "response": {
        "id": "resp_cut", "status": "failed", "error": {"code": code, "message": message}}})


TOO_LONG = "Your input exceeds the context window of this model. Please adjust your input and try again."


def completed(output: list[dict]) -> bytes:
    return sse("response.completed", {"type": "response.completed", "response": {
        "id": "resp_cut", "status": "completed", "output": output,
        "usage": {"input_tokens": 5, "output_tokens": 7}}})


async def source(chunks: list[bytes], *, then: Exception | None = None, pause: float = 0.0):
    """``chunks``, ``pause`` seconds apart."""
    for index, chunk in enumerate(chunks):
        if pause and index:
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


OPENING = {"id": "resp_bridge", "object": "response", "status": "in_progress", "output": []}


def run(chunks, *, then=None, pause=0.0, keepalive_every=None, tools=True):
    """Output events, and the error the stream ended with (if any).

    With ``keepalive_every`` the output goes through ``kept_alive`` as well.
    """
    transform = excel_tool_stream_transform(SOURCE_BODY) if tools else None
    out: list[bytes] = []

    async def go():
        stream = source(chunks, then=then, pause=pause)
        if transform is not None:
            stream = transform(stream)
        if keepalive_every is not None:
            stream = kept_alive(stream, OPENING, keepalive_every)
        async for chunk in stream:
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
    """Codex drops a stream it hears nothing from for five minutes and sends the request again."""

    def names(self, events):
        return [name for name, _ in events]

    def test_silent_upstream_gets_in_progress_repeated(self):
        output = [{"type": "message", "id": "msg_0", "role": "assistant", "phase": "final_answer",
                   "content": [{"type": "output_text", "text": "Done."}]}]
        chunks = [created(), *message(0, "Done.", "final_answer"), completed(output)]
        events, error = run(chunks, pause=0.2, keepalive_every=0.05)
        self.assertIsNone(error)
        names = self.names(events)
        self.assertEqual(names[0], "response.created")
        self.assertGreater(names.count("response.in_progress"), len(chunks))
        self.assertEqual(names[-1], "response.completed")
        beat = next(payload for name, payload in events if name == "response.in_progress")
        self.assertEqual(beat["response"]["id"], "resp_cut")

    def test_a_backend_silent_before_its_first_event_is_announced_once(self):
        output = [{"type": "message", "id": "msg_0", "role": "assistant", "phase": "final_answer",
                   "content": [{"type": "output_text", "text": "Done."}]}]
        in_progress = sse("response.in_progress", {"type": "response.in_progress",
                                                   "response": {"id": "resp_cut", "status": "in_progress"}})
        chunks = [created(), in_progress, *message(0, "Done.", "final_answer"), completed(output)]

        async def late():
            await asyncio.sleep(0.3)
            for chunk in chunks:
                yield chunk

        async def go():
            return [chunk async for chunk in kept_alive(excel_tool_stream_transform(SOURCE_BODY)(late()),
                                                        OPENING, 0.05)]

        events = parse(b"".join(asyncio.run(go())))
        names = self.names(events)
        self.assertEqual(names[0], "response.created")
        self.assertEqual(events[0][1]["response"]["id"], "resp_bridge")
        self.assertEqual(names.count("response.created"), 1)
        beats = [payload["response"]["id"] for name, payload in events if name == "response.in_progress"]
        self.assertGreaterEqual(len(beats), 2)
        self.assertEqual(set(beats), {"resp_bridge"})
        self.assertEqual(names[-1], "response.completed")

    def test_a_tool_call_held_back_while_it_streams_is_kept_alive(self):
        # The upstream is never quiet for long, yet Codex hears nothing of the
        # call until it is whole.
        call = officejs_call(0, "ls")
        arguments = json.loads(call[1].decode().split("data: ", 1)[1])["delta"]
        pieces = [arguments[i:i + 8] for i in range(0, len(arguments), 8)]
        deltas = [sse("response.function_call_arguments.delta", {
            "type": "response.function_call_arguments.delta", "output_index": 0, "item_id": "fc_1",
            "delta": piece}) for piece in pieces]
        item = json.loads(call[3].decode().split("data: ", 1)[1])["item"]
        chunks = [created(), call[0], *deltas, call[2], call[3], completed([item])]
        events, error = run(chunks, pause=0.02, keepalive_every=0.1)
        self.assertIsNone(error)
        self.assertGreater(len(chunks) * 0.02, 0.4)
        self.assertGreaterEqual(self.names(events).count("response.in_progress"), 2)
        calls = [payload["item"] for name, payload in events
                 if name == "response.output_item.done" and payload["item"]["type"] == "function_call"]
        self.assertEqual([(call["name"], json.loads(call["arguments"])) for call in calls],
                         [("shell_command", {"command": "ls"})])

    def test_a_stream_without_tools_is_kept_alive_and_passed_on_as_it_came(self):
        # A compaction declares no tools, so nothing else looks at its events.
        output = [{"type": "message", "id": "msg_0", "role": "assistant", "content": []}]
        whole = [created(), *message(0, "Summary.", "final_answer"), completed(output), b"data: [DONE]\n\n"]
        raw = b"".join(whole)
        # The opening and part of the next event, then silence; the rest in odd pieces.
        split = len(whole[0]) + 10
        chunks = [raw[:split], raw[split:split + 27], raw[split + 27:split + 200], raw[split + 200:]]

        async def quiet():
            yield chunks[0]
            await asyncio.sleep(0.2)
            for chunk in chunks[1:]:
                yield chunk

        async def go():
            return [chunk async for chunk in kept_alive(quiet(), OPENING, 0.05)]

        out = asyncio.run(go())
        events = parse(b"".join(out))
        names = self.names(events)
        self.assertEqual(names[0], "response.created")
        self.assertEqual(events[0][1]["response"]["id"], "resp_cut")
        self.assertGreaterEqual(names.count("response.in_progress"), 2)
        self.assertEqual(b"".join(chunk for chunk in out if b"response.in_progress" not in chunk), raw)

    def test_crlf_events_are_passed_on(self):
        crlf = [block.replace(b"\n", b"\r\n") for block in (created(), completed([]))]
        raw = b"".join(crlf)
        # A line break split between two reads, too.
        cut_at = raw.index(b"\r\n\r\n") + 1
        events, error = run([raw[:cut_at], raw[cut_at:]], keepalive_every=15.0, tools=False)
        self.assertIsNone(error)
        self.assertEqual(self.names(events), ["response.created", "response.completed"])

    def test_no_keepalive_after_the_last_event(self):
        output = [{"type": "message", "id": "msg_0", "role": "assistant", "content": []}]

        async def lingering():
            yield created()
            yield completed(output)
            await asyncio.sleep(0.3)

        async def go():
            return [chunk async for chunk in kept_alive(lingering(), OPENING, 0.05)]

        self.assertEqual(self.names(parse(b"".join(asyncio.run(go())))),
                         ["response.created", "response.completed"])

    def test_an_event_the_connection_broke_off_after_is_still_whole(self):
        output = [{"type": "message", "id": "msg_0", "role": "assistant", "content": []}]
        events, error = run([created(), completed(output).rstrip(b"\n")], then=cut(),
                            keepalive_every=15.0, tools=False)
        self.assertIsNone(error)
        self.assertEqual(self.names(events), ["response.created", "response.completed"])

    def test_a_cut_off_event_is_left_out(self):
        events, error = run([created(), completed([])[:40]], then=cut(), keepalive_every=15.0, tools=False)
        self.assertIsNone(error)
        self.assertEqual(self.names(events), ["response.created", "response.failed"])


class UnfinishedStreamTests(unittest.TestCase):
    """Codex says only "stream closed before response.completed" for a stream that just stops."""

    def last(self, chunks, *, then=None, tools=True):
        events, error = run(chunks, then=then, keepalive_every=15.0, tools=tools)
        self.assertIsNone(error)
        self.assertEqual([name for name, _ in events].count("response.failed"), 1)
        self.assertEqual(events[-1][0], "response.failed")
        return events[-1][1]["response"]

    def test_a_backend_breaking_off_mid_answer_is_told_to_codex(self):
        for tools in (True, False):
            with self.subTest(tools=tools):
                response = self.last([created(), *message(0, "Half of the answ", "final_answer")[:2]],
                                     then=cut(), tools=tools)
                self.assertEqual((response["id"], response["status"]), ("resp_cut", "failed"))
                # A code Codex does not know: it shows the message and sends the request again.
                self.assertEqual(response["error"]["code"], "upstream_error")
                self.assertIn("closed the connection 0 s into the answer", response["error"]["message"])
                self.assertIn("RemoteProtocolError: peer closed connection", response["error"]["message"])

    def test_what_the_backend_reported_before_breaking_off_reaches_codex(self):
        # Codex ignores ``error`` events; as response.failed a context window
        # exceeded makes it compact the conversation.
        for tools in (True, False):
            with self.subTest(tools=tools):
                response = self.last([created(), error_event("context_length_exceeded", TOO_LONG)],
                                     then=cut(), tools=tools)
                self.assertEqual(response["error"], {"code": "context_length_exceeded", "message": TOO_LONG})

    def test_the_error_in_the_responses_api_shape_is_read_too(self):
        chunk = sse("error", {"type": "error", "code": "server_error", "message": "boom", "param": None})
        self.assertEqual(self.last([created(), chunk], then=cut())["error"],
                         {"code": "server_error", "message": "boom"})

    def test_the_backends_own_failure_is_passed_on_alone(self):
        response = self.last([created(), error_event("server_error", "boom"), failed("server_error", "boom")])
        self.assertEqual(response, {"id": "resp_cut", "status": "failed",
                                    "error": {"code": "server_error", "message": "boom"}})

    def test_a_stream_that_just_ends_is_told_to_codex(self):
        response = self.last([created(), *message(0, "Half of the answ", "final_answer")[:2]], tools=False)
        self.assertEqual(response["error"]["code"], "upstream_error")
        self.assertIn("ended the answer 0 s in, before finishing it", response["error"]["message"])

    def test_a_stream_that_never_opened_is_told_with_the_bridges_response(self):
        response = self.last([], then=cut(), tools=False)
        self.assertEqual(response["id"], "resp_bridge")

    def test_a_failure_of_the_bridge_itself_is_told_and_raised(self):
        events, error = run([created()], then=ValueError("bug"), keepalive_every=15.0, tools=False)
        self.assertIsInstance(error, ValueError)
        self.assertEqual(events[-1][0], "response.failed")
        self.assertIn("The bridge failed while passing on the answer (ValueError: bug)",
                      events[-1][1]["response"]["error"]["message"])


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

    def test_codex_is_told_why_when_upstream_breaks_off(self):
        cases = {
            "mid-answer": ([created(), *message(0, "Half of the answ", "final_answer")[:2]], "upstream_error"),
            "after an error": ([created(), error_event("context_length_exceeded", TOO_LONG)],
                               "context_length_exceeded"),
        }
        for name, (body, code) in cases.items():
            for tools in (True, False):
                with self.subTest(name, tools=tools):
                    def handler(_request, body=body):
                        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                              stream=BrokenBody(body))

                    reader = StaticReader()
                    reader.store.configure(session_headers(None), persist=False, allow_expired=True)
                    app = create_app(reader, client_factory=lambda handler=handler: httpx.AsyncClient(
                        transport=httpx.MockTransport(handler)))
                    request = {**(SOURCE_BODY if tools else {"model": "gpt-5.6-sol-excel"}),
                               "input": "draw", "stream": True}

                    async def go(app=app, request=request):
                        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
                        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
                            return await client.post("/v1/responses", json=request)

                    response = asyncio.run(go())
                    self.assertEqual(response.status_code, 200)
                    events = parse(response.content)
                    self.assertEqual(events[-1][0], "response.failed")
                    self.assertEqual(events[-1][1]["response"]["error"]["code"], code)


class SlowBody(httpx.AsyncByteStream):
    """``(seconds to wait, chunk)`` pairs."""

    def __init__(self, steps: list[tuple[float, bytes]]):
        self.steps = steps

    async def __aiter__(self):
        for wait, chunk in self.steps:
            await asyncio.sleep(wait)
            yield chunk


class BridgeKeepaliveTests(unittest.TestCase):
    OUTPUT = [{"type": "message", "id": "msg_0", "role": "assistant", "phase": "final_answer",
               "content": [{"type": "output_text", "text": "Done."}]}]

    def ask(self, steps: list[tuple[float, bytes]], body: dict) -> list[tuple[str, dict]]:
        def handler(_request):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=SlowBody(steps))

        reader = StaticReader()
        reader.store.configure(session_headers(None), persist=False, allow_expired=True)
        app = create_app(reader, client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))

        async def go():
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
                return await client.post("/v1/responses", json={**body, "input": "go on", "stream": True})

        with mock.patch.object(excel_stream, "KEEPALIVE_SECONDS", 0.05):
            response = asyncio.run(go())
        self.assertEqual(response.status_code, 200)
        return parse(response.content)

    def test_codex_hears_a_quiet_compaction(self):
        # Codex compacts a conversation with a request that declares no tools.
        steps = [(0, created()), (0.3, b"".join(message(0, "Done.", "final_answer"))), (0, completed(self.OUTPUT))]
        events = self.ask(steps, {"model": "gpt-5.6-sol-excel"})
        names = [name for name, _ in events]
        self.assertEqual(names[0], "response.created")
        self.assertGreaterEqual(names.count("response.in_progress"), 2)
        self.assertEqual(names[-1], "response.completed")

    def test_codex_hears_a_backend_silent_before_its_first_event(self):
        steps = [(0.3, created()), (0, b"".join(message(0, "Done.", "final_answer"))), (0, completed(self.OUTPUT))]
        events = self.ask(steps, SOURCE_BODY)
        names = [name for name, _ in events]
        self.assertEqual(names[0], "response.created")
        self.assertEqual(names.count("response.created"), 1)
        self.assertNotEqual(events[0][1]["response"]["id"], "resp_cut")
        self.assertGreaterEqual(names.count("response.in_progress"), 2)
        self.assertEqual(names[-1], "response.completed")


if __name__ == "__main__":
    unittest.main()

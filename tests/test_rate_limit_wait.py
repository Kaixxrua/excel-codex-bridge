"""The bridge waits out the backend's rate limit before Codex sees it.

The add-in's users share one tokens-per-minute budget per model.  When it is
used up the stream fails at once, saying to try again in some milliseconds;
Codex does, five times within a second, and then gives up on the turn.
"""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest import mock

import httpx

from excel_codex_bridge import server
from excel_codex_bridge.server import create_app

from helpers import session_headers
from test_server import StaticReader, text_stream

TOOLS = [{"type": "function", "name": "shell_command", "parameters": {"type": "object"}}]
LIMITED = ("Rate limit reached for gpt-6-astra in organization org-test on tokens per min (TPM): "
           "Limit 500000000, Used 499990000, Requested 150000. Please try again in 18ms.")


def sse(event: str, payload: dict, newline: str = "\n") -> bytes:
    return f"event: {event}{newline}data: {json.dumps(payload)}{newline}{newline}".encode()


def created(newline: str = "\n") -> bytes:
    return sse("response.created", {"type": "response.created",
                                     "response": {"id": "resp_limited", "status": "in_progress"}}, newline)


def failed(code: str = "rate_limit_exceeded", message: str = LIMITED, newline: str = "\n") -> bytes:
    return sse("response.failed", {"type": "response.failed", "response": {
        "id": "resp_limited", "status": "failed", "error": {"code": code, "message": message}}}, newline)


def error_event(code: str = "rate_limit_exceeded", message: str = LIMITED) -> bytes:
    """Basispoints says it first, then ``response.failed``."""
    return sse("error", {"type": "error", "error": {"type": "tokens", "code": code, "message": message,
                                                     "param": None}, "sequence_number": 2})


def stream(body: bytes) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)


class Upstream:
    """Answers each request with the next of ``answers``, the last one from then on."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers[min(len(self.requests), len(self.answers)) - 1]
        return answer if isinstance(answer, httpx.Response) else stream(answer)


def ask(upstream: Upstream, *, tools: bool = True, wait: str | None = None, delays=(0.01,)) -> str:
    reader = StaticReader()
    reader.store.configure(session_headers(None), persist=False, allow_expired=True)
    app = create_app(reader, client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    body = {"model": "gpt-6-astra-excel", "input": "ping", "stream": True, **({"tools": TOOLS} if tools else {})}

    async def go():
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
            return await client.post("/v1/responses", json=body)

    environ = {} if wait is None else {"EXCEL_BRIDGE_RATE_LIMIT_WAIT": wait}
    with mock.patch.dict(os.environ, environ), mock.patch.object(server, "RATE_LIMIT_DELAYS", delays):
        response = asyncio.run(go())
    assert response.status_code == 200, response.text
    return response.text


def event_names(text: str) -> list[str]:
    return [line[len("event: "):] for line in text.replace("\r\n", "\n").split("\n") if line.startswith("event: ")]


def events(text: str) -> list[dict]:
    return [json.loads(line[len("data: "):]) for line in text.replace("\r\n", "\n").split("\n")
            if line.startswith("data: ") and line != "data: [DONE]"]


class WaitingTests(unittest.TestCase):
    def test_a_rate_limited_try_is_sent_again_and_codex_gets_the_answer(self):
        for tools in (True, False):
            with self.subTest(tools=tools):
                upstream = Upstream(created() + failed(), text_stream("pong"))
                text = ask(upstream, tools=tools)
                names = event_names(text)
                self.assertEqual(len(upstream.requests), 2)
                self.assertEqual(upstream.requests[0].content, upstream.requests[1].content)
                self.assertNotIn("response.failed", names)
                self.assertEqual(names.count("response.created"), 1)
                self.assertEqual(names[0], "response.created")
                self.assertIn("resp_limited", text.split("\n\n")[0])
                self.assertEqual(names[-1], "response.completed")
                self.assertIn("pong", text)

    def test_a_rate_limit_said_in_an_error_event_first_is_waited_out_too(self):
        for opening in (created(), b""):
            with self.subTest(opened=bool(opening)):
                upstream = Upstream(opening + error_event() + failed(), text_stream("pong"))
                text = ask(upstream, tools=False)
                names = event_names(text)
                self.assertEqual(len(upstream.requests), 2)
                self.assertNotIn("response.failed", names)
                self.assertNotIn("error", names)
                self.assertEqual(names.count("response.created"), 1)
                self.assertEqual(events(text)[0]["response"]["status"], "in_progress")
                self.assertEqual(names[-1], "response.completed")

    def test_a_rate_limit_said_in_an_error_event_that_outlasts_the_wait_is_told_to_codex(self):
        upstream = Upstream(created() + error_event() + failed())
        text = ask(upstream, wait="0.05")
        self.assertGreater(len(upstream.requests), 2)
        failure = next(e for e in events(text) if e["type"] == "response.failed")
        self.assertEqual(failure["response"]["error"]["code"], "invalid_prompt")
        self.assertIn("Please try again in 18ms", failure["response"]["error"]["message"])

    def test_once_the_wait_is_used_up_codex_is_told_so_and_not_to_try_again(self):
        upstream = Upstream(created() + failed())
        text = ask(upstream, wait="0.05")
        self.assertGreater(len(upstream.requests), 2)
        self.assertEqual(event_names(text), ["response.created", "response.failed"])
        response = events(text)[-1]["response"]
        # Codex tries a rate limit again, which the bridge would wait out afresh; not this code.
        self.assertEqual(response["error"]["code"], "invalid_prompt")
        self.assertIn("still rate limited after 0.05 seconds", response["error"]["message"])
        self.assertIn("Please try again in 18ms", response["error"]["message"])
        self.assertEqual((response["id"], response["status"]), ("resp_limited", "failed"))

    def test_no_wait_when_it_is_turned_off(self):
        upstream = Upstream(created() + failed(), text_stream("pong"))
        text = ask(upstream, wait="0")
        self.assertEqual(len(upstream.requests), 1)
        self.assertEqual(event_names(text), ["response.created", "response.failed"])
        self.assertEqual(events(text)[-1]["response"]["error"]["code"], "rate_limit_exceeded")

    def test_other_failures_are_not_sent_again(self):
        upstream = Upstream(created() + failed("server_error", "boom"), text_stream("pong"))
        text = ask(upstream)
        self.assertEqual(len(upstream.requests), 1)
        self.assertIn("boom", text)

    def test_nothing_is_sent_again_once_the_answer_has_begun(self):
        item = {"type": "message", "id": "msg_1", "role": "assistant", "status": "in_progress", "content": []}
        begun = created() + sse("response.output_item.added", {
            "type": "response.output_item.added", "output_index": 0, "item": item})
        upstream = Upstream(begun + failed(), text_stream("pong"))
        text = ask(upstream, tools=False)
        self.assertEqual(len(upstream.requests), 1)
        self.assertEqual(event_names(text), ["response.created", "response.output_item.added", "response.failed"])

    def test_the_earlier_failure_reaches_codex_when_the_next_try_is_refused(self):
        refused = httpx.Response(500, json={"error": {"message": "down"}})
        upstream = Upstream(created() + failed(), refused)
        text = ask(upstream, tools=False)
        self.assertEqual(len(upstream.requests), 2)
        self.assertEqual(event_names(text), ["response.created", "response.failed"])
        self.assertIn("Please try again in 18ms", text)
        self.assertEqual(events(text)[-1]["response"]["error"]["code"], "rate_limit_exceeded")

    def test_a_failure_before_any_opening_event_opens_the_stream_itself(self):
        upstream = Upstream(failed(), text_stream("pong"))
        text = ask(upstream, tools=False)
        names = event_names(text)
        self.assertEqual(len(upstream.requests), 2)
        self.assertEqual(names[0], "response.created")
        self.assertEqual(names.count("response.created"), 1)
        opened = events(text)[0]["response"]
        self.assertEqual((opened["id"], opened["status"]), ("resp_limited", "in_progress"))
        self.assertNotIn("error", opened)
        self.assertEqual(names[-1], "response.completed")

    def test_codex_hears_the_stream_is_still_going_while_the_bridge_waits(self):
        for tools in (True, False):
            with self.subTest(tools=tools):
                upstream = Upstream(created() + failed(), text_stream("pong"))
                with mock.patch.object(server, "RATE_LIMIT_KEEPALIVE", 0.02):
                    text = ask(upstream, tools=tools, delays=(0.1,))
                names = event_names(text)
                self.assertEqual(names[0], "response.created")
                self.assertGreaterEqual(names.count("response.in_progress"), 3)
                self.assertTrue(all(event["response"]["id"] == "resp_limited" for event in events(text)
                                    if event.get("type") == "response.in_progress"))
                self.assertEqual(names[-1], "response.completed")

    def test_crlf_streams_are_read_too(self):
        upstream = Upstream(created("\r\n") + failed(newline="\r\n"), text_stream("pong"))
        text = ask(upstream, tools=False)
        self.assertEqual(len(upstream.requests), 2)
        self.assertNotIn("response.failed", event_names(text))


class ClosingTests(unittest.TestCase):
    def test_no_further_try_once_codex_has_gone(self):
        upstream = Upstream(created() + failed(), text_stream("pong"))

        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
                async def send():
                    return await client.send(client.build_request("POST", "http://upstream.test/"), stream=True)

                source = server._PastRateLimits(await send(), send, 60.0)
                opening = await source.__anext__()
                # As the keepalive does: this read waits out the rate limit in a task of its own.
                reading = asyncio.ensure_future(source.__anext__())
                await asyncio.sleep(0.05)
                await source.aclose()
                with self.assertRaises(StopAsyncIteration):
                    await asyncio.wait_for(reading, 5)
                return opening

        with mock.patch.object(server, "RATE_LIMIT_DELAYS", (0.2,)):
            opening = asyncio.run(go())
        self.assertIn(b"response.created", opening)
        self.assertEqual(len(upstream.requests), 1)


class DelayTests(unittest.TestCase):
    def test_delays(self):
        delay = server._rate_limit_delay
        self.assertEqual(delay(LIMITED, 0, 300.0), 1.0)
        self.assertEqual(delay(LIMITED, 3, 60.0), 8.0)
        self.assertEqual(delay(LIMITED, 20, 60.0), 15.0)
        self.assertEqual(delay("Rate limit reached. Please try again in 7s.", 0, 60.0), 7.0)
        self.assertEqual(delay("Please try again in 2.5 seconds", 1, 60.0), 2.5)
        self.assertEqual(delay(LIMITED, 4, 0.5), 0.5)
        self.assertIsNone(delay(LIMITED, 0, 0.0))

    def test_the_wait_setting(self):
        for value, expected in (("", 300.0), ("abc", 300.0), ("nan", 300.0), ("0", 0.0), ("-5", 0.0),
                                ("90", 90.0), ("1000", 1000.0), ("5000", 1800.0), ("inf", 1800.0)):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"EXCEL_BRIDGE_RATE_LIMIT_WAIT": value}):
                self.assertEqual(server.rate_limit_wait(), expected)


if __name__ == "__main__":
    unittest.main()

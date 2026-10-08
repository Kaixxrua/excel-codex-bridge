from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx

from excel_codex_bridge import cli, codex_config, route_check, sse, upstream_routes
from excel_codex_bridge.native_upstream import BASE_URL, NativeBridge
from excel_codex_bridge.server import create_app
from excel_codex_bridge.session import SessionReader
from excel_codex_bridge.sub2api import GatewayKeys, create_app as sidecar
from helpers import session_headers, write_codex_login

MODEL = "gpt-5.6-sol"
CATALOG = {"models": [{"slug": MODEL, "visibility": "list", "context_window": 272000,
                       "base_instructions": "Native instructions", "supported_reasoning_levels": [{"effort": "low"}]}]}


def done(output=None, status="completed"):
    return {"id": "resp_native", "object": "response", "model": MODEL, "status": status,
            "reasoning": {"effort": "low"}, "output": output or []}


def stream(response=None, prefix=b""):
    response = response or done()
    return prefix + sse.sse_encode("response." + response["status"],
                                  {"type": "response." + response["status"], "response": response})


class NativeRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.auth = write_codex_login(self.root / "auth.json", time.time() + 3600)
        self.reader = SessionReader(login="codex", codex_auth=self.auth)
        self.reader.refresh(force=True)
        self.requests = []

    def bridge(self, handler):
        def record(request):
            self.requests.append(request)
            return handler(request)
        bridge = NativeBridge(self.reader, lambda: httpx.AsyncClient(transport=httpx.MockTransport(record)))
        self.addAsyncCleanup(bridge.aclose)
        return bridge

    async def test_native_tools_and_encrypted_history_are_preserved(self):
        history = [{"role": "user", "content": "task"},
                   {"type": "reasoning", "encrypted_content": "opaque"},
                   {"type": "custom_tool_call", "name": "apply_patch", "call_id": "call_1", "input": "patch"},
                   {"type": "custom_tool_call_output", "call_id": "call_1", "output": "done"}]
        body = {"model": MODEL, "instructions": "original instructions", "reasoning": {"effort": "max"},
                "input": history, "tools": [{"type": "custom", "name": "apply_patch"}], "stream": False}
        bridge = self.bridge(lambda r: httpx.Response(200, content=stream()))
        response = await bridge.responses(body)
        self.assertEqual(response.status_code, 200)
        request = self.requests[0]
        sent = json.loads(request.content)
        self.assertEqual(str(request.url), BASE_URL + "/responses")
        for key in ("instructions", "reasoning", "input", "tools"):
            self.assertEqual(sent[key], body[key])
        self.assertTrue(sent["stream"])
        self.assertFalse(sent["store"])
        self.assertNotIn("x-basispoints-auth-mode", request.headers)
        self.assertNotIn("x-openai-account-id", request.headers)
        self.assertNotIn("run_officejs", request.content.decode())
        self.assertEqual(response.headers["x-excel-bridge-route"], "codex")

    async def test_forbidden_and_redirected_requests_are_not_retried(self):
        for status in (302, 401, 403, 429):
            with self.subTest(status=status):
                self.requests.clear()
                bridge = self.bridge(lambda r: httpx.Response(status, headers={"location": "https://example.invalid/", "retry-after": "12"},
                    json={"error": {"code": "denied", "message": "upstream refused"}}))
                response = await bridge.responses({"model": MODEL, "input": "hi"})
                self.assertEqual(len(self.requests), 1)
                self.assertEqual(response.status_code, status if status >= 400 else 502)
                self.assertEqual(response.headers["retry-after"], "12")
                self.assertEqual(self.reader.source, "codex")

    async def test_stream_without_terminal_event_fails_even_after_a_finished_item(self):
        partial = sse.sse_encode("response.output_item.done", {"type": "response.output_item.done",
            "output_index": 0, "item": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "answer"}]}})
        bridge = self.bridge(lambda r: httpx.Response(200, content=partial))
        response = await bridge.responses({"model": MODEL, "input": "hi", "stream": True})
        events = b"".join([chunk async for chunk in response.body_iterator])
        self.assertIn(b"response.failed", events)
        self.assertNotIn(b"response.completed", events)
        self.assertEqual(len(self.requests), 1)

    async def test_failed_or_incomplete_nonstream_is_not_success(self):
        for status in ("failed", "incomplete"):
            bridge = self.bridge(lambda r: httpx.Response(200, content=stream(done(status=status))))
            response = await bridge.responses({"model": MODEL, "input": "hi"})
            self.assertEqual(response.status_code, 502)
            self.assertEqual(json.loads(response.body)["status"], status)

    async def test_malformed_completed_event_is_a_failure(self):
        for item in ({"type": "response.completed", "response": {"status": "in_progress"}},
                     {"type": "response.output_text.delta", "delta": "unfinished"},
                     {"type": []}):
            with self.subTest(item=item):
                bad = sse.sse_encode("response.completed", item)
                bridge = self.bridge(lambda r: httpx.Response(200, content=bad))
                response = await bridge.responses({"model": MODEL, "input": "hi", "stream": True})
                events = b"".join([chunk async for chunk in response.body_iterator])
                self.assertIn(b"response.failed", events)
                self.assertNotIn(b"response.completed", events)

    async def test_cancelled_stream_closes_upstream(self):
        closed = False
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield sse.sse_encode("response.created", {"type": "response.created", "response": done(status="in_progress")})
                await asyncio.Event().wait()
            async def aclose(self):
                nonlocal closed
                closed = True
        bridge = self.bridge(lambda r: httpx.Response(200, stream=Stream()))
        response = await bridge.responses({"model": MODEL, "input": "hi", "stream": True})
        await response.body_iterator.__anext__()
        await response.body_iterator.aclose()
        self.assertTrue(closed)

    async def test_done_items_complete_the_buffered_output_without_altering_them(self):
        item = {"type": "function_call", "namespace": "fixture", "name": "read", "id": "fc_1",
                "call_id": "call_1", "arguments": "{}"}
        prefix = sse.sse_encode("response.output_item.done", {"type": "response.output_item.done", "output_index": 0, "item": item})
        bridge = self.bridge(lambda r: httpx.Response(200, content=stream(prefix=prefix)))
        response = await bridge.responses({"model": MODEL, "input": "hi"})
        self.assertEqual(json.loads(response.body)["output"], [item])

    async def test_account_switch_cannot_reuse_a_live_conversation(self):
        bridge = self.bridge(lambda r: httpx.Response(200, content=stream()))
        body = {"model": MODEL, "input": "hi", "prompt_cache_key": "thread-1"}
        self.assertEqual((await bridge.responses(body)).status_code, 200)
        write_codex_login(self.auth, time.time() + 3600, account="other-account")
        self.reader.refresh(force=True)
        self.assertEqual((await bridge.responses(body)).status_code, 409)
        self.assertEqual(len(self.requests), 1)

    async def test_catalog_is_account_specific_and_keeps_native_capabilities(self):
        bridge = self.bridge(lambda r: httpx.Response(200, json=CATALOG))
        self.assertEqual(await bridge.catalog(), CATALOG)
        self.assertEqual(await bridge.catalog(), CATALOG)
        self.assertEqual(len(self.requests), 1)
        write_codex_login(self.auth, time.time() + 7200, account="other-account")
        self.reader.refresh(force=True)
        self.assertEqual(await bridge.catalog(), CATALOG)
        self.assertEqual(len(self.requests), 2)

    async def test_excel_aliases_and_server_state_are_refused_before_sending(self):
        bridge = self.bridge(lambda r: self.fail("must not contact upstream"))
        for extra in ({"model": MODEL + "-1m-excel"}, {"store": True}, {"previous_response_id": "resp_old"}):
            response = await bridge.responses({"model": MODEL, "input": "hi", **extra})
            self.assertEqual(response.status_code, 400)

    async def test_local_api_selects_native_and_keeps_loopback_guard(self):
        factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=stream())))
        app = create_app(self.reader, client_factory=factory, route="codex")
        self.addAsyncCleanup(app.app.state.bridge.aclose)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)), base_url="http://127.0.0.1") as client:
            response = await client.post("/v1/responses", json={"model": MODEL, "input": "hi"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["x-excel-bridge-route"], "codex")
            self.assertFalse((await client.get("/healthz")).json()["inference_verified"])
            denied = await client.post("/v1/responses", json={}, headers={"Origin": "https://example.com"})
            self.assertEqual(denied.status_code, 403)

    async def test_sidecar_requires_codex_source_for_native_transport(self):
        keys = GatewayKeys("a" * 32, "b" * 32)
        app = sidecar(keys, route="codex")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)), base_url="http://127.0.0.1") as client:
            headers = {"Authorization": "Bearer " + keys.admin}
            for source in (None, "excel"):
                response = await client.post("/admin/session", headers=headers, json={"headers": session_headers(), "source": source})
                self.assertEqual(response.status_code, 400)
            response = await client.post("/admin/session", headers=headers, json={"headers": session_headers(), "source": "codex"})
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("Bearer", response.text)

    async def test_check_records_two_calls_without_credentials_or_task_data(self):
        count = 0
        def handler(request):
            nonlocal count
            if request.method == "GET":
                return httpx.Response(200, json=CATALOG)
            count += 1
            sent = json.loads(request.content)
            if count == 1:
                output = [{"type": "reasoning", "encrypted_content": "sealed-secret"},
                          {"type": "function_call", "namespace": "bridge_probe", "name": "read_value", "call_id": "call_1", "arguments": "{}"}]
            else:
                self.assertEqual(sent["input"][1]["encrypted_content"], "sealed-secret")
                value = json.loads(sent["input"][-1]["output"])["value"]
                output = [{"type": "message", "content": [{"type": "output_text", "text": value}]}]
            return httpx.Response(200, content=stream(done(output)))
        receipt = self.root / "receipt.json"
        report = await route_check.run(self.reader, lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                                       model=MODEL, effort="low", receipt=receipt)
        self.assertTrue(report["protocol_verified"])
        self.assertFalse(report["quality_advantage_proven"])
        self.assertEqual(count, 2)
        saved = receipt.read_text()
        for secret in ("sealed-secret", "Bearer", "access_token", "fixture value", "codex-account"):
            self.assertNotIn(secret, saved)
        if os.name != "nt":
            self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)

    async def test_failed_check_stops_after_one_request(self):
        count = 0
        def handler(request):
            nonlocal count
            if request.method == "GET":
                return httpx.Response(200, json=CATALOG)
            count += 1
            return httpx.Response(403, json={"error": {"code": "denied"}})
        report = await route_check.run(self.reader, lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                                       model=MODEL, effort="low", receipt=self.root / "failed.json")
        self.assertEqual(count, 1)
        self.assertFalse(report["protocol_verified"])

    async def test_timed_out_check_keeps_the_reserved_submission(self):
        async def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json=CATALOG)
            await asyncio.Event().wait()
        receipt = self.root / "timeout.json"
        report = await route_check.run(self.reader, lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                                       model=MODEL, effort="low", receipt=receipt, timeout=0.01)
        self.assertFalse(report["protocol_verified"])
        self.assertEqual(len(report["calls"]), 1)
        self.assertEqual(json.loads(receipt.read_text())["calls"][0]["status"], "completion_unknown")


class RouteConfigTests(unittest.TestCase):
    def test_default_is_native_and_legacy_route_is_explicit(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(upstream_routes.ENV, None)
            self.assertEqual(cli._parser().parse_args(["serve"]).route, "codex")
            self.assertEqual(cli._parser().parse_args(["serve", "--route", "excel"]).route, "excel")
            self.assertEqual(cli._reader(cli._parser().parse_args(["serve"])).login, "codex")

    def test_native_catalog_is_separate_and_keeps_original_limits(self):
        root = Path(tempfile.mkdtemp())
        path = codex_config.write_catalog(root, payload=CATALOG, name="codex-native-model-catalog.json")
        self.assertEqual(json.loads(path.read_text()), CATALOG)
        self.assertFalse((root / codex_config.CATALOG_NAME).exists())
        args = codex_config.codex_overrides(8765, path, excel=False)
        self.assertNotIn("x-openai-actor-authorization", " ".join(args))

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx

from excel_codex_bridge import cli, codex_login
from excel_codex_bridge.server import create_app
from excel_codex_bridge.session import SessionReader

from helpers import write_codex_login, write_webview_session
from test_server import text_stream

BODY = {"model": "gpt-5.6-sol-excel", "input": "hi", "stream": True}


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "auth.json"

    def test_headers_carry_the_chatgpt_sign_in(self):
        write_codex_login(self.path, time.time() + 3600, chatgpt_account_user_id="user-1__codex-account")
        headers = codex_login.load(self.path)
        self.assertTrue(headers["authorization"].startswith("Bearer "))
        self.assertEqual(headers["chatgpt-account-id"], "codex-account")
        self.assertEqual(headers["x-openai-account-id"], "codex-account")
        self.assertEqual(headers["x-openai-account-user-id"], "user-1__codex-account")
        self.assertEqual(headers["x-basispoints-auth-mode"], "chatgpt")

    def test_account_id_comes_from_the_token_when_the_file_has_none(self):
        write_codex_login(self.path, time.time() + 3600, account=None)
        self.assertEqual(codex_login.load(self.path)["chatgpt-account-id"], "claims-account")

    def test_an_api_key_sign_in_is_not_enough(self):
        self.path.write_text(json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "sk-test", "tokens": None}))
        with self.assertRaisesRegex(ValueError, "API key"):
            codex_login.load(self.path)

    def test_missing_or_broken_files_say_so(self):
        with self.assertRaisesRegex(ValueError, "not signed in"):
            codex_login.load(self.path)
        self.path.write_text("{not json")
        with self.assertRaisesRegex(ValueError, "not a Codex login file"):
            codex_login.load(self.path)

    def test_where_the_file_is(self):
        with mock.patch.dict(os.environ, {codex_login.AUTH_FILE_ENV: "", "CODEX_HOME": "/somewhere/codex"}):
            self.assertEqual(codex_login.auth_path(), Path("/somewhere/codex/auth.json"))
        with mock.patch.dict(os.environ, {codex_login.AUTH_FILE_ENV: "", "CODEX_HOME": ""}):
            self.assertEqual(codex_login.auth_path(), Path.home() / ".codex" / "auth.json")
        with mock.patch.dict(os.environ, {codex_login.AUTH_FILE_ENV: "/explicit/auth.json"}):
            self.assertEqual(codex_login.auth_path(), Path("/explicit/auth.json"))


class ReaderTests(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.auth = root / "codex" / "auth.json"
        self.webview = root / "webview"

    def reader(self, login: str | None = None) -> SessionReader:
        return SessionReader(webview_root=self.webview, login=login, codex_auth=self.auth)

    def account(self, reader: SessionReader) -> str:
        return reader.store.request_headers(stream=False)["chatgpt-account-id"]

    def test_codex_sign_in_comes_first(self):
        write_codex_login(self.auth, time.time() + 3600)
        write_webview_session(self.webview, time.time() + 3600, account="excel-account")
        reader = self.reader()
        status = reader.refresh(force=True)
        self.assertEqual((status["source"], status["reader"]), ("codex", "codex-auth-json"))
        self.assertEqual(self.account(reader), "codex-account")
        self.assertNotIn("Bearer", json.dumps(status))

    def test_excel_session_when_codex_is_not_signed_in(self):
        write_webview_session(self.webview, time.time() + 3600, account="excel-account")
        reader = self.reader()
        status = reader.refresh(force=True)
        self.assertEqual(status["source"], "excel")
        self.assertEqual(self.account(reader), "excel-account")
        self.assertIn("not signed in", status["notes"])

    def test_excel_session_when_the_codex_sign_in_ran_out(self):
        write_codex_login(self.auth, time.time() - 60)
        write_webview_session(self.webview, time.time() + 3600, account="excel-account")
        status = self.reader().refresh(force=True)
        self.assertEqual(status["source"], "excel")
        self.assertIn("codex: expired", status["notes"])

    def test_an_expired_codex_sign_in_is_still_reported_when_there_is_nothing_else(self):
        write_codex_login(self.auth, time.time() - 60)
        status = self.reader().refresh(force=True)
        self.assertEqual((status["source"], status["configured"], status["expired"]), ("codex", True, True))

    def test_codex_back_in_front_once_it_signs_in(self):
        write_webview_session(self.webview, time.time() + 3600, account="excel-account")
        reader = self.reader()
        self.assertEqual(reader.refresh(force=True)["source"], "excel")
        write_codex_login(self.auth, time.time() + 3600)
        self.assertEqual(reader.refresh(force=True)["source"], "codex")

    def test_one_sign_in_only_when_asked(self):
        write_codex_login(self.auth, time.time() + 3600)
        write_webview_session(self.webview, time.time() + 3600, account="excel-account")
        self.assertEqual(self.reader("excel").refresh(force=True)["source"], "excel")
        self.auth.unlink()
        status = self.reader("codex").refresh(force=True)
        self.assertFalse(status["configured"])
        self.assertIn("not signed in", status["error"])

    def test_login_from_the_environment(self):
        with mock.patch.dict(os.environ, {"EXCEL_BRIDGE_LOGIN": "excel"}):
            self.assertEqual(self.reader().login, "excel")
        with mock.patch.dict(os.environ, {"EXCEL_BRIDGE_LOGIN": "nonsense"}):
            self.assertEqual(self.reader().login, "auto")

    def test_a_refused_codex_sign_in_waits_for_a_new_one(self):
        write_codex_login(self.auth, time.time() + 3600)
        write_webview_session(self.webview, time.time() + 3600, account="excel-account")
        reader = self.reader()
        reader.refresh(force=True)
        self.assertTrue(reader.fall_back("codex"))
        self.assertEqual((reader.source, self.account(reader)), ("excel", "excel-account"))
        self.assertIn("refused", reader.refresh(force=True)["notes"])
        self.assertEqual(reader.source, "excel")
        # `codex login` writes the file again.
        write_codex_login(self.auth, time.time() + 7200, account="codex-account-2")
        self.assertEqual(reader.refresh(force=True)["source"], "codex")
        self.assertEqual(self.account(reader), "codex-account-2")

    def test_no_fall_back_without_an_excel_session_or_outside_auto(self):
        write_codex_login(self.auth, time.time() + 3600)
        reader = self.reader()
        reader.refresh(force=True)
        self.assertFalse(reader.fall_back("codex"))
        self.assertEqual(reader.source, "codex")
        self.assertEqual(reader.refresh(force=True)["source"], "codex")
        write_webview_session(self.webview, time.time() + 3600, account="excel-account")
        only_codex = self.reader("codex")
        only_codex.refresh(force=True)
        self.assertFalse(only_codex.fall_back("codex"))
        self.assertFalse(reader.fall_back("excel"))

    def test_hints_name_the_sign_in(self):
        write_codex_login(self.auth, time.time() + 3600)
        reader = self.reader()
        reader.refresh(force=True)
        self.assertIn("codex login", reader.hint())
        self.assertNotIn("Excel", reader.hint())
        self.assertIn("add-in pane", self.reader("excel").hint())
        self.auth.unlink()
        nothing = self.reader()
        nothing.refresh(force=True)
        self.assertIn("codex login", nothing.hint())
        self.assertIn("add-in pane", nothing.hint())


class FallBackRouteTests(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.auth = write_codex_login(root / "codex" / "auth.json", time.time() + 3600)
        write_webview_session(root / "webview", time.time() + 3600, account="excel-account")
        self.reader = SessionReader(webview_root=root / "webview", codex_auth=self.auth)
        self.accounts: list[str] = []

    def app(self, handler):
        def record(request: httpx.Request) -> httpx.Response:
            self.accounts.append(request.headers["chatgpt-account-id"])
            return handler(request)

        return create_app(
            self.reader,
            client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(record), timeout=5.0),
        )

    def post(self, app, path: str = "/v1/responses", body: dict = BODY) -> httpx.Response:
        async def run():
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
                return await client.post(path, json=body)

        return asyncio.run(run())

    def test_a_refused_codex_sign_in_is_retried_with_the_excel_session(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.headers["chatgpt-account-id"] == "codex-account":
                return httpx.Response(401, json={"error": {"message": "not for this client"}})
            return httpx.Response(200, content=text_stream("pong"), headers={"content-type": "text/event-stream"})

        app = self.app(handler)
        response = self.post(app)
        self.assertEqual(response.status_code, 200)
        self.assertIn("pong", response.text)
        self.assertEqual(self.accounts, ["codex-account", "excel-account"])
        # From now on straight to the Excel session.
        self.assertEqual(self.post(app).status_code, 200)
        self.assertEqual(self.accounts[2:], ["excel-account"])

    def test_the_image_tool_falls_back_too(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.headers["chatgpt-account-id"] == "codex-account":
                return httpx.Response(403, json={"error": {"message": "not for this client"}})
            return httpx.Response(200, json={"created": 1, "data": [{"b64_json": "AAAA"}]})

        response = self.post(self.app(handler), "/v1/images/generations", {"prompt": "a whale"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.accounts, ["codex-account", "excel-account"])

    def test_when_both_are_refused_the_excel_answer_comes_back(self):
        app = self.app(lambda _r: httpx.Response(401, json={"error": {"message": "nope"}}))
        response = self.post(app)
        self.assertEqual(response.status_code, 401)
        message = response.json()["error"]["message"]
        self.assertIn("the Excel add-in's ChatGPT session", message)
        self.assertIn("add-in pane", message)
        self.assertEqual(self.accounts, ["codex-account", "excel-account"])

    def test_other_errors_do_not_switch(self):
        app = self.app(lambda _r: httpx.Response(429, json={"error": {"message": "slow down"}}))
        self.assertEqual(self.post(app).status_code, 429)
        self.assertEqual(self.accounts, ["codex-account"])
        self.assertEqual(self.reader.source, "codex")

    def test_only_codex_says_to_sign_in_to_codex_again(self):
        self.reader = SessionReader(login="codex", codex_auth=self.auth)
        app = self.app(lambda _r: httpx.Response(401, json={"error": {"message": "nope"}}))
        response = self.post(app)
        self.assertEqual(response.status_code, 401)
        message = response.json()["error"]["message"]
        self.assertIn("Codex's ChatGPT sign-in", message)
        self.assertIn("codex login", message)
        self.assertEqual(self.accounts, ["codex-account"])


class DescribeTests(unittest.TestCase):
    def test_codex_sign_in(self):
        ok, message = cli._describe_session({
            "configured": True, "expired": False, "expires_at": time.time() + 7200,
            "login": "auto", "source": "codex", "codex_auth": "/home/me/.codex/auth.json", "notes": "",
        })
        self.assertTrue(ok)
        self.assertIn("Using Codex's ChatGPT sign-in (/home/me/.codex/auth.json); expires", message)

    def test_excel_session_says_why_codex_was_passed_over(self):
        ok, message = cli._describe_session({
            "configured": True, "expired": False, "expires_at": None, "login": "auto",
            "source": "excel", "notes": "codex: Codex is not signed in",
        })
        self.assertTrue(ok)
        self.assertIn("Using the Excel add-in's ChatGPT session.", message)
        self.assertIn("Not using Codex's sign-in: Codex is not signed in", message)

    def test_nothing_usable_points_at_both(self):
        ok, message = cli._describe_session({"configured": False, "login": "auto", "error": "x"})
        self.assertFalse(ok)
        self.assertIn("codex login", message)
        self.assertIn("excel-codex login", message)
        ok, message = cli._describe_session({
            "configured": True, "expired": True, "login": "codex", "source": "codex",
        })
        self.assertFalse(ok)
        self.assertIn("Codex's ChatGPT sign-in has expired", message)
        self.assertNotIn("excel-codex login", message)


if __name__ == "__main__":
    unittest.main()

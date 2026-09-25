from __future__ import annotations

import http.server
import io
import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from excel_codex_bridge import __version__, cli, updates

from helpers import write_webview_session

API_ANSWER = {
    "tag_name": "v9.1.0",
    "name": "excel-codex-bridge v9.1.0",
    "html_url": "https://github.com/Kaixxrua/excel-codex-bridge/releases/tag/v9.1.0",
    "draft": False,
    "prerelease": False,
    "body": "\n修好了 **某个问题** · Fixed \x1b[2Ja thing\n\n> Unofficial.\n",
}


class VersionTests(unittest.TestCase):
    def test_is_newer(self):
        for version, current, expected in [
            ("0.4.4", "0.4.3", True),
            ("v0.4.10", "0.4.9", True),
            ("0.5", "0.4.9", True),
            ("0.4.3", "0.4.3", False),
            ("0.4.3.0", "0.4.3", False),
            ("0.4.2", "0.4.3", False),
            ("nightly", "0.4.3", False),
        ]:
            with self.subTest(version=version, current=current):
                self.assertEqual(updates.is_newer(version, current), expected)

    def test_release_from_api_answer(self):
        release = updates.release_from(API_ANSWER)
        self.assertEqual(release.version, "9.1.0")
        self.assertEqual(release.url, API_ANSWER["html_url"])
        # First line of the notes, as plain text on one line.
        self.assertEqual(release.summary, "修好了 某个问题 · Fixed [2Ja thing")

    def test_unusable_answers(self):
        for change in ({"draft": True}, {"prerelease": True}, {"tag_name": "latest"}, {"tag_name": None}):
            with self.subTest(change=change):
                self.assertIsNone(updates.release_from({**API_ANSWER, **change}))
        self.assertIsNone(updates.release_from(["not", "a", "release"]))
        elsewhere = updates.release_from({**API_ANSWER, "html_url": "https://example.com/x"})
        self.assertEqual(elsewhere.url, updates.RELEASES_PAGE)


class UpdateCheckTests(unittest.TestCase):
    def setUp(self):
        self.cache = Path(tempfile.mkdtemp()) / "state" / updates.CACHE_NAME
        self.now = 1_000_000.0
        self.answers: list[updates.Release | None] = []
        self.fetches = 0

    def fetch(self):
        self.fetches += 1
        return self.answers.pop(0)

    def check(self) -> updates.UpdateCheck:
        return updates.UpdateCheck(self.cache, fetch=self.fetch, every=3600, clock=lambda: self.now)

    def test_asks_github_at_most_every_interval(self):
        release = updates.Release("9.1.0", "summary", "https://github.com/x")
        self.answers = [release, updates.Release("9.2.0", "", "https://github.com/y")]
        self.assertEqual(self.check().latest(), release)
        self.now += 3599
        self.assertEqual(self.check().latest(), release)
        self.assertEqual(self.fetches, 1)
        self.now += 2
        self.assertEqual(self.check().latest().version, "9.2.0")
        self.assertEqual(self.fetches, 2)

    def test_unreachable_github_keeps_what_was_known(self):
        release = updates.Release("9.1.0", "", "https://github.com/x")
        self.answers = [release, None, None]
        self.check().latest()
        self.now += 7200
        self.assertEqual(self.check().latest(), release)
        # Nothing new was learned, so the next start asks again.
        self.assertEqual(self.check().latest(), release)
        self.assertEqual(self.fetches, 3)

    def test_broken_cache_is_ignored(self):
        self.cache.parent.mkdir(parents=True)
        self.cache.write_text("{not json")
        self.answers = [None]
        self.assertIsNone(self.check().latest())

    def test_newer_skips_this_version(self):
        self.answers = [updates.Release(__version__, "", "https://github.com/x")]
        self.assertIsNone(self.check().newer())


class _Api(http.server.BaseHTTPRequestHandler):
    status = 200
    seen: list[dict] = []

    def do_GET(self):
        type(self).seen.append(dict(self.headers))
        body = json.dumps(API_ANSWER).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Api)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/latest"
        _Api.status, _Api.seen = 200, []
        clean = {key: "" for key in os.environ if key.lower().endswith("_proxy")}
        patcher = mock.patch.dict(os.environ, {**clean, "EXCEL_BRIDGE_PROXY": "", "NO_PROXY": "*"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_reads_the_latest_release(self):
        release = updates.fetch_latest(self.url)
        self.assertEqual(release.version, "9.1.0")
        headers = {key.lower(): value for key, value in _Api.seen[0].items()}
        self.assertEqual(headers["user-agent"], f"excel-codex-bridge/{__version__}")

    def test_failures_are_quiet(self):
        _Api.status = 500
        self.assertIsNone(updates.fetch_latest(self.url))
        self.server.shutdown()
        self.server.server_close()
        self.assertIsNone(updates.fetch_latest(self.url))


class BackgroundTests(unittest.TestCase):
    def check(self, fetch) -> updates.UpdateCheck:
        cache = Path(tempfile.mkdtemp()) / updates.CACHE_NAME
        return updates.UpdateCheck(cache, fetch=fetch, every=0.01)

    def test_watch_announces_each_new_version_once(self):
        answers = iter(
            [updates.Release("9.1.0", "", "u")] * 5 + [updates.Release("9.2.0", "", "u")] * 1000
        )
        announced = []
        updates.watch(self.check(lambda: next(answers)), lambda release: announced.append(release.version))
        deadline = time.monotonic() + 5
        while len(announced) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.1)
        self.assertEqual(announced, ["9.1.0", "9.2.0"])

    def test_in_background_does_not_wait_unless_asked(self):
        gate = threading.Event()

        def slow_fetch():
            gate.wait(5)
            return updates.Release("9.1.0", "", "u")

        wait = updates.in_background(self.check(slow_fetch))
        self.assertIsNone(wait(0))
        gate.set()
        self.assertEqual(wait(5).version, "9.1.0")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        patcher = mock.patch.dict(
            os.environ,
            {"EXCEL_BRIDGE_HOME": str(self.root / "bridge-home"), "CODEX_HOME": str(self.root / "codex-home")},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(cli._update_shown.clear)
        for name in ("SIGTERM", "SIGBREAK", "SIGHUP"):
            if hasattr(cli.signal, name):
                sig = getattr(cli.signal, name)
                self.addCleanup(cli.signal.signal, sig, cli.signal.getsignal(sig))
        self.release = updates.Release("9.1.0", "", "https://github.com/Kaixxrua/excel-codex-bridge/releases/tag/v9.1.0")
        self.check = updates.UpdateCheck(self.root / "update-check.json", fetch=lambda: self.release)

    def test_launcher_shows_the_notice_after_codex_exits(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        while_codex_ran = []

        def fake_codex(command, env):
            while_codex_ran.append(stderr.getvalue())
            return 0

        bridge = mock.Mock()
        bridge.poll.return_value = None
        with mock.patch.object(cli, "_update_check", lambda: self.check), \
                mock.patch.object(updates, "in_background", lambda check: lambda timeout: check.newer()), \
                mock.patch.object(cli, "_find_codex", lambda explicit: "codex"), \
                mock.patch.object(cli, "_ensure_session", lambda reader, args: True), \
                mock.patch.object(cli, "_healthy", lambda port: True), \
                mock.patch.object(cli.subprocess, "Popen", lambda *args, **kwargs: bridge), \
                mock.patch.object(cli.subprocess, "call", fake_codex), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(cli.main(["codex"]), 0)
        self.assertEqual(while_codex_ran, [""])
        self.assertIn("Update available: 9.1.0", stderr.getvalue())
        self.assertNotIn("Update available", stdout.getvalue())

    def test_desktop_window_shows_the_notice_while_running(self):
        webview = write_webview_session(self.root / "webview", time.time() + 3 * 86400)
        output = io.StringIO()

        def fake_bridge(reader, args, *, host, port, quiet):
            deadline = time.monotonic() + 5
            while not cli._update_shown.is_set() and time.monotonic() < deadline:
                time.sleep(0.01)
            raise KeyboardInterrupt

        with mock.patch.object(cli, "_update_check", lambda: self.check), \
                mock.patch.object(cli, "_run_bridge", fake_bridge), \
                mock.patch.object(cli, "_port_free", lambda port: True), \
                redirect_stdout(output):
            self.assertEqual(cli.main(["desktop", "--webview-dir", str(webview), "--no-auto-signin"]), 0)
        text = output.getvalue()
        self.assertIn("Update available: 9.1.0", text)
        self.assertLess(text.index("Listening on"), text.index("Update available"))

    def test_off_by_environment(self):
        with mock.patch.dict(os.environ, {"EXCEL_BRIDGE_UPDATE_CHECK": "0"}):
            self.assertIsNone(cli._update_check())
        with mock.patch.dict(os.environ, {"EXCEL_BRIDGE_UPDATE_CHECK": "", "EXCEL_BRIDGE_HOME": "/nowhere"}):
            self.assertEqual(cli._update_check().cache, Path("/nowhere") / updates.CACHE_NAME)

    def test_notice_names_both_versions_and_the_link(self):
        release = updates.Release("9.1.0", "修好了 · Fixed", "https://github.com/Kaixxrua/excel-codex-bridge/releases/tag/v9.1.0")
        output = io.StringIO()
        with redirect_stdout(output):
            cli._announce_update(release)
            cli._announce_update(None)
        self.assertEqual(
            output.getvalue(),
            f"Update available: 9.1.0 (you have {__version__}).\n"
            "  修好了 · Fixed\n"
            "  -> Download: https://github.com/Kaixxrua/excel-codex-bridge/releases/tag/v9.1.0\n",
        )
        self.assertTrue(cli._update_shown.is_set())


if __name__ == "__main__":
    unittest.main()

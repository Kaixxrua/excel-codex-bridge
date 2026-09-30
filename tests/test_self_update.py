from __future__ import annotations

import hashlib
import http.server
import io
import os
import re
import subprocess
import tempfile
import threading
import time
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from excel_codex_bridge import __version__, cli, self_update, updates

REPO = Path(__file__).resolve().parents[1]
NEW = "9.1.0"


def release_zip(version: str = NEW, *, top: bool = True, extra: dict[str, bytes] | None = None) -> bytes:
    prefix = f"excel-codex-bridge-{version}-windows-x64/" if top else ""
    files = {
        "excel-codex.exe": b"new exe",
        "excel-codex-desktop.cmd": b"@echo off\r\n",
        "_internal/python.dll": b"new dll",
        "README.md": b"new readme",
        "docs/sub2api.md": b"new doc",
        **(extra or {}),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        if top:
            archive.writestr(prefix, b"")
        archive.writestr(prefix + "docs/", b"")
        for name, data in files.items():
            archive.writestr(prefix + name, data)
    return buffer.getvalue()


def api_answer(zip_bytes: bytes, url: str, version: str = NEW, **asset) -> dict:
    name = f"excel-codex-bridge-{version}-windows-x64.zip"
    return {
        "tag_name": f"v{version}",
        "html_url": f"https://github.com/Kaixxrua/excel-codex-bridge/releases/tag/v{version}",
        "draft": False,
        "prerelease": False,
        "body": "Fixed a thing",
        "assets": [
            {"name": name + ".sha256", "digest": "sha256:" + "0" * 64, "size": 1, "browser_download_url": url + ".sha256"},
            {
                "name": name,
                "digest": "sha256:" + hashlib.sha256(zip_bytes).hexdigest(),
                "size": len(zip_bytes),
                "browser_download_url": url,
                **asset,
            },
        ],
    }


class _Files(http.server.BaseHTTPRequestHandler):
    body = b""
    delay = 0.0
    hits = 0

    def do_GET(self):
        type(self).hits += 1
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        for start in range(0, len(self.body), 1024):
            time.sleep(self.delay)
            try:
                self.wfile.write(self.body[start:start + 1024])
            except OSError:
                return

    def log_message(self, *args):
        pass


def started(version: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0, stdout=f"excel-codex.exe {version}\n", stderr="")


class DownloadFromTests(unittest.TestCase):
    def test_picks_the_windows_zip_and_its_digest(self):
        data = release_zip()
        download = self_update.download_from(api_answer(data, "https://github.com/x.zip"))
        self.assertEqual(download.version, NEW)
        self.assertEqual(download.url, "https://github.com/x.zip")
        self.assertEqual(download.sha256, hashlib.sha256(data).hexdigest())
        self.assertEqual(download.size, len(data))

    def test_nothing_it_cannot_check(self):
        data = release_zip()
        for broken in ({"digest": None}, {"digest": "md5:abc"}, {"size": 0}, {"browser_download_url": "file:///x"}):
            self.assertIsNone(self_update.download_from(api_answer(data, "https://github.com/x.zip", **broken)))
        answer = api_answer(data, "https://github.com/x.zip")
        answer["assets"][1]["name"] = f"excel-codex-bridge-{NEW}-macos-arm64.tar.gz"
        self.assertIsNone(self_update.download_from(answer))
        self.assertIsNone(self_update.download_from({**answer, "draft": True}))

    def test_mirror_goes_in_front(self):
        url = "https://github.com/Kaixxrua/excel-codex-bridge/releases/download/v1/x.zip"
        with mock.patch.dict(os.environ, {"EXCEL_BRIDGE_DOWNLOAD_MIRROR": "https://mirror.example"}):
            self.assertEqual(self_update.mirrored(url), "https://mirror.example/" + url)
        with mock.patch.dict(os.environ, {"EXCEL_BRIDGE_DOWNLOAD_MIRROR": "mirror.example"}):
            self.assertEqual(self_update.mirrored(url), url)
        with mock.patch.dict(os.environ, {"EXCEL_BRIDGE_DOWNLOAD_MIRROR": ""}):
            self.assertEqual(self_update.mirrored(url), url)


class UnpackTests(unittest.TestCase):
    def unpack(self, data: bytes) -> Path:
        root = Path(tempfile.mkdtemp())
        (root / "r.zip").write_bytes(data)
        self_update.unpack(root / "r.zip", root / "new")
        return root / "new"

    def test_drops_the_top_folder(self):
        for top in (True, False):
            folder = self.unpack(release_zip(top=top))
            self.assertEqual((folder / "excel-codex.exe").read_bytes(), b"new exe")
            self.assertEqual((folder / "_internal" / "python.dll").read_bytes(), b"new dll")
            self.assertTrue((folder / "docs" / "sub2api.md").is_file())

    def test_refuses_paths_outside_the_folder(self):
        for name in ("../escape.txt", "/abs.txt", "C:/x.txt", "a/../../b.txt"):
            with self.subTest(name=name):
                with self.assertRaises(self_update.Failed):
                    self.unpack(release_zip(top=False, extra={name: b"x"}))

    def test_refuses_what_is_not_the_windows_package(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("top/excel-codex", b"mac build")
        with self.assertRaisesRegex(self_update.Failed, "not the Windows package"):
            self.unpack(buffer.getvalue())


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.app = self.root / "excel codex (1)"
        (self.app / "_internal").mkdir(parents=True)
        (self.app / "_internal" / "python.dll").write_bytes(b"old dll")
        (self.app / "excel-codex.exe").write_bytes(b"old exe")
        (self.app / "excel-codex-desktop.cmd").write_bytes(b"old launcher")
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Files)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/excel-codex-bridge-{NEW}-windows-x64.zip"
        _Files.body, _Files.delay, _Files.hits = release_zip(), 0.0, 0
        self.answer = api_answer(_Files.body, self.url)
        clean = {key: "" for key in os.environ if key.lower().endswith("_proxy")}
        patcher = mock.patch.dict(
            os.environ,
            {**clean, "EXCEL_BRIDGE_PROXY": "", "NO_PROXY": "*", "EXCEL_BRIDGE_DOWNLOAD_MIRROR": "",
             self_update.JUST_UPDATED: ""},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.now = 1_000_000.0
        self.ran: list[dict] = []
        self.run_result = started(NEW)
        self.output = io.StringIO()
        self.busy = False

    def run_new(self, command, **kwargs):
        self.ran.append({"command": command, **kwargs})
        return self.run_result

    def installer(self, **overrides) -> self_update.Installer:
        options = dict(
            state=self.root / "state",
            current="0.5.13",
            fetch=lambda: self_update.download_from(self.answer),
            run=self.run_new,
            stream=self.output,
            clock=lambda: self.now,
            busy=lambda: self.busy,
        )
        options.update(overrides)
        return self_update.Installer(self.app, **options)

    def stage(self) -> Path:
        return self.app / self_update.STAGE_NAME

    def assert_untouched(self):
        self.assertEqual((self.app / "excel-codex.exe").read_bytes(), b"old exe")
        self.assertEqual((self.app / "_internal" / "python.dll").read_bytes(), b"old dll")
        self.assertEqual((self.app / "excel-codex-desktop.cmd").read_bytes(), b"old launcher")

    def test_newer_release_is_unpacked_next_to_the_install(self):
        self.assertEqual(self.installer().launcher(), self_update.UPDATE_READY)
        stage = self.stage()
        self.assertEqual((stage / "new" / "excel-codex.exe").read_bytes(), b"new exe")
        self.assertEqual((stage / "new" / "_internal" / "python.dll").read_bytes(), b"new dll")
        self.assertEqual((stage / "apply.cmd").read_bytes(), self_update.APPLY_CMD)
        self.assertEqual((stage / "version.txt").read_text(), NEW)
        self.assertFalse((stage / "download.zip").exists())
        self.assert_untouched()
        # The new program ran once from where it was unpacked, as its own PyInstaller build.
        [ran] = self.ran
        self.assertEqual(ran["command"], [str(stage / "new" / "excel-codex.exe"), "--version"])
        self.assertEqual(ran["env"]["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        self.assertIn(f"{NEW} is out (you have 0.5.13). Downloading it", self.output.getvalue())

    def test_what_was_unpacked_is_used_again_even_offline(self):
        self.installer().launcher()
        self.assertEqual(_Files.hits, 1)
        self.assertEqual(self.installer().launcher(), self_update.UPDATE_READY)
        self.assertEqual(self.installer(fetch=lambda: None).launcher(), self_update.UPDATE_READY)
        self.assertEqual(_Files.hits, 1)
        self.assertEqual(len(self.ran), 1)

    def test_nothing_newer_cleans_up(self):
        self.installer().launcher()
        self.assertEqual(self.installer(current=NEW).launcher(), 0)
        self.assertFalse(self.stage().exists())
        self.installer().launcher()
        self.assertEqual(self.installer(current="9.2", fetch=lambda: None).launcher(), 0)
        self.assertFalse(self.stage().exists())

    def test_wrong_checksum_starts_this_version_and_waits_an_hour(self):
        self.answer["assets"][1]["digest"] = "sha256:" + "1" * 64
        self.assertEqual(self.installer().launcher(), 0)
        self.assertFalse(self.stage().exists())
        self.assertIn("does not match the SHA-256 GitHub lists", self.output.getvalue())
        self.assertIn("tried again in an hour", self.output.getvalue())
        self.assert_untouched()
        self.assertEqual(self.ran, [])

        self.now += 1800
        self.assertEqual(self.installer().launcher(), 0)
        self.assertEqual(_Files.hits, 1)
        self.assertIn("installing it failed a moment ago", self.output.getvalue())

        self.now += 1801
        self.answer = api_answer(_Files.body, self.url)
        self.assertEqual(self.installer().launcher(), self_update.UPDATE_READY)
        self.assertEqual(_Files.hits, 2)

    def test_download_cut_short_is_refused(self):
        self.answer["assets"][1]["size"] = len(_Files.body) + 10
        self.assertEqual(self.installer().launcher(), 0)
        self.assertIn("the download stopped at", self.output.getvalue())
        self.assertFalse(self.stage().exists())

    def test_new_program_that_does_not_start_is_not_installed(self):
        self.run_result = subprocess.CompletedProcess([], 1, stdout="", stderr="boom")
        self.assertEqual(self.installer().launcher(), 0)
        self.assertIn("the new excel-codex.exe did not start (exit code 1)", self.output.getvalue())
        self.assertFalse(self.stage().exists())
        # A build that calls itself another version is not what GitHub announced either.
        self.run_result = started("9.0.9")
        self.now += 3601
        self.assertEqual(self.installer().launcher(), 0)
        self.assertFalse(self.stage().exists())

    def test_escape_skips_this_time_only(self):
        _Files.body = release_zip(extra={"_internal/big.bin": os.urandom(64 * 1024)})
        _Files.delay = 0.01
        self.answer = api_answer(_Files.body, self.url)
        presses = iter([False, False, True])
        installer = self.installer(escape=lambda: next(presses, True))
        self.assertEqual(installer.launcher(), 0)
        self.assertIn("Press Esc to skip", self.output.getvalue())
        self.assertIn("Skipped for now.", self.output.getvalue())
        self.assertFalse(self.stage().exists())
        self.assertFalse((self.root / "state" / self_update.STATE_NAME).exists())

    def test_waits_while_another_copy_runs_from_this_folder(self):
        self.busy = True
        self.assertEqual(self.installer().launcher(), 0)
        self.assertIn("still running from this folder", self.output.getvalue())
        self.assertEqual(self.installer().stage.joinpath("version.txt").read_text(), NEW)
        self.busy = False
        self.assertEqual(self.installer().launcher(), self_update.UPDATE_READY)
        self.assertEqual(_Files.hits, 1)

    def test_after_apply_cleans_up_and_says_so(self):
        self.installer().launcher()
        (self.stage() / "old").mkdir()
        with mock.patch.dict(os.environ, {self_update.JUST_UPDATED: "later"}):
            self.assertEqual(self.installer(fetch=self.fail).launcher(), 0)
        self.assertTrue((self.stage() / "version.txt").exists())
        with mock.patch.dict(os.environ, {self_update.JUST_UPDATED: __version__}):
            self.assertEqual(self.installer(fetch=self.fail).launcher(), 0)
        self.assertFalse(self.stage().exists())
        self.assertIn(f"Updated to {__version__}.", self.output.getvalue())

    def fail(self):
        raise AssertionError("must not ask GitHub")

    def test_by_hand(self):
        self.assertEqual(self.installer(current=NEW).by_hand(), 0)
        self.assertIn(f"You have the newest release ({NEW}).", self.output.getvalue())
        self.assertEqual(self.installer().by_hand(), 0)
        self.assertIn("installs the next time you start excel-codex-desktop.cmd", self.output.getvalue())
        self.assertEqual(self.installer(fetch=lambda: None).by_hand(), 1)


def _labels(text: str) -> set[str]:
    return {line[1:].strip().lower() for line in text.splitlines() if line.startswith(":")}


class BatchFileTests(unittest.TestCase):
    """cmd.exe is not here: keep both files to what is safe to hand around."""

    def check(self, raw: bytes) -> str:
        raw.decode("ascii")
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        text = raw.decode("ascii")
        labels = _labels(text)
        for target in re.findall(r"(?:goto|call) :?([A-Za-z_]+)", text, flags=re.IGNORECASE):
            if target.lower() != "eof":
                self.assertIn(target.lower(), labels)
        for line in text.splitlines():
            # A ( ) block is parsed at once, and a ")" in the folder's path would end it.
            if not line.lower().startswith(("rem", "for ")):
                self.assertNotIn("(", line)
        return text

    def test_apply_cmd(self):
        text = self.check(self_update.APPLY_CMD)
        # %~dp0 inside a `call :label` is not this file; only the top may use it.
        first_label = text.index("\r\n:")
        self.assertNotIn("%~dp0", text[first_label:])
        # Both ways out start the launcher again, with the loop guard set.
        self.assertEqual(text.count('set "EXCEL_BRIDGE_JUST_UPDATED='), 2)

    def test_desktop_launcher(self):
        text = self.check((REPO / "excel-codex-desktop.cmd").read_bytes())
        self.assertIn('"%~dp0excel-codex.exe" update --launcher', text)
        self.assertIn("if errorlevel 10 if not errorlevel 11 goto install", text)
        # Handed over without `call`, so apply.cmd may replace this file.
        self.assertIn('\r\n"%~dp0.update\\apply.cmd" %*', text)

    def test_restore_launcher(self):
        text = self.check((REPO / "excel-codex-restore.cmd").read_bytes())
        self.assertIn('"%~dp0excel-codex.exe" restore %*', text)
        self.assertIn('call "%~dp0excel-codex.cmd" restore %*', text)
        # Nothing is downloaded on the way.
        commands = [line for line in text.splitlines() if not line.lower().startswith("rem")]
        self.assertFalse([line for line in commands if "update" in line])
        # Double-clicked, the window stays until what it did is read.
        self.assertIn("\r\npause\r\n", text)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(cli._update_shown.clear)

    def test_launcher_step_from_source_does_nothing(self):
        with mock.patch.object(self_update, "fetch_download", side_effect=AssertionError), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["update", "--launcher"]), 0)
        self.assertEqual(output.getvalue(), "")

    def test_by_hand_from_source(self):
        release = updates.Release("9.1.0", "", "https://github.com/Kaixxrua/excel-codex-bridge/releases/tag/v9.1.0")
        with mock.patch.object(updates, "fetch_latest", lambda: release), redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["update"]), 0)
        self.assertIn("Update available: 9.1.0", output.getvalue())
        self.assertIn("`git pull` updates it", output.getvalue())

    def test_notice_when_the_package_installs_it(self):
        release = updates.Release("9.1.0", "", "https://github.com/Kaixxrua/excel-codex-bridge/releases/tag/v9.1.0")
        text = updates.notice(release, installs_itself=True)
        self.assertIn("installs itself the next time you start excel-codex-desktop.cmd", text)
        self.assertNotIn("Download:", text)
        text.encode("ascii")

    def test_off_by_environment(self):
        for name in ("EXCEL_BRIDGE_AUTO_UPDATE", "EXCEL_BRIDGE_UPDATE_CHECK"):
            with mock.patch.dict(os.environ, {"EXCEL_BRIDGE_AUTO_UPDATE": "", "EXCEL_BRIDGE_UPDATE_CHECK": "", name: "0"}):
                self.assertFalse(self_update.enabled())
        with mock.patch.dict(os.environ, {"EXCEL_BRIDGE_AUTO_UPDATE": "", "EXCEL_BRIDGE_UPDATE_CHECK": ""}):
            self.assertTrue(self_update.enabled())
            self.assertFalse(self_update.installs_itself())  # not the frozen Windows package


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import os
import posixpath
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from unittest import mock

from excel_codex_bridge import cli, excel_signin

WE = "{http://schemas.microsoft.com/office/webextensions/webextension/2010/11}"
WETP = "{http://schemas.microsoft.com/office/webextensions/taskpanes/2010/11}"
REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
CT = "{http://schemas.openxmlformats.org/package/2006/content-types}"


def status(expires_in: float | None, *, configured: bool = True) -> dict:
    if not configured:
        return {"configured": False, "expired": False, "expires_at": None}
    return {"configured": True, "expired": expires_in <= 0, "expires_at": time.time() + expires_in}


class FakeReader:
    method = "webview2-localstorage-leveldb"

    def __init__(self, *statuses: dict) -> None:
        self.statuses = list(statuses)
        self.reads = 0

    def refresh(self, *, force: bool = False) -> dict:
        self.reads += 1
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]

    def status(self) -> dict:
        return self.statuses[0]


class FakeExcel:
    def __init__(self, running: bool = False) -> None:
        self.running = running
        self.opened: list[tuple[Path, bool]] = []
        self.closed: list[tuple[Path, bool]] = []

    def signin(self, reader, **kwargs) -> excel_signin.ExcelSignIn:
        clock = iter(range(0, 10_000, 2))
        return excel_signin.ExcelSignIn(
            reader,
            enabled=True,
            workbook_dir=Path(tempfile.mkdtemp()),
            opener=lambda path, *, minimized: self.opened.append((path, minimized)),
            closer=lambda path, *, quit_excel: self.closed.append((path, quit_excel)) or True,
            excel_running=lambda: self.running,
            sleep=lambda _: None,
            clock=lambda: next(clock),
            **kwargs,
        )


class WorkbookTests(unittest.TestCase):
    def test_workbook_embeds_the_chatgpt_pane(self):
        path = excel_signin.write_workbook(Path(tempfile.mkdtemp()))
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            parts = {name: ET.fromstring(archive.read(name)) for name in names}

        overrides = {o.get("PartName").lstrip("/") for o in parts["[Content_Types].xml"].iter(f"{CT}Override")}
        self.assertEqual(overrides, names - {"[Content_Types].xml", "_rels/.rels"} - {
            n for n in names if n.endswith(".rels")
        })
        for name in (n for n in names if n.endswith(".rels")):
            base = posixpath.dirname(posixpath.dirname(name))
            for rel in parts[name].iter(f"{REL}Relationship"):
                self.assertIn(posixpath.normpath(posixpath.join(base, rel.get("Target"))), names, name)

        reference = parts["xl/webextensions/webextension1.xml"].find(f"{WE}reference")
        self.assertEqual(reference.get("id"), "WA200010215")
        self.assertEqual(reference.get("storeType"), "OMEX")
        prop = parts["xl/webextensions/webextension1.xml"].find(f"{WE}properties/{WE}property")
        self.assertEqual((prop.get("name"), prop.get("value")), ("Office.AutoShowTaskpaneWithDocument", "true"))
        pane = parts["xl/webextensions/taskpanes.xml"].find(f"{WETP}taskpane")
        self.assertEqual(pane.get("visibility"), "1")

    def test_rewriting_an_identical_workbook_leaves_it_alone(self):
        directory = Path(tempfile.mkdtemp())
        first = excel_signin.write_workbook(directory)
        stamp = first.stat().st_mtime_ns
        time.sleep(0.01)
        self.assertEqual(excel_signin.write_workbook(directory), first)
        self.assertEqual(first.stat().st_mtime_ns, stamp)
        self.assertEqual(sorted(p.name for p in directory.iterdir()), [excel_signin.WORKBOOK_NAME])


class SignInTests(unittest.TestCase):
    def test_signing_in_shows_excel_and_closes_it_again(self):
        excel = FakeExcel(running=False)
        fresh = status(10 * 86400)
        reader = FakeReader(status(0, configured=False), status(0, configured=False), fresh)
        result = excel.signin(reader).run(interactive=True, timeout=60)
        self.assertIs(result, fresh)
        self.assertEqual([minimized for _, minimized in excel.opened], [False])
        # The bridge started Excel, so it may quit it once the workbook is closed.
        self.assertEqual(excel.closed, [(excel.opened[0][0], True)])

    def test_refresh_waits_for_a_newer_token_and_leaves_a_running_excel(self):
        excel = FakeExcel(running=True)
        old = status(3600)
        reader = FakeReader(old, old, status(10 * 86400))
        result = excel.signin(reader).run(
            interactive=False, timeout=60, previous_expiry=excel_signin.expiry(old)
        )
        self.assertIsNotNone(result)
        self.assertEqual(reader.reads, 3)
        self.assertEqual(excel.opened[0][1], True)
        self.assertEqual(excel.closed[0][1], False)

    def test_timeout_still_closes_the_workbook(self):
        excel = FakeExcel()
        result = excel.signin(FakeReader(status(0, configured=False))).run(interactive=True, timeout=10)
        self.assertIsNone(result)
        self.assertEqual(len(excel.closed), 1)

    def test_disabled_off_windows_and_by_environment(self):
        reader = FakeReader(status(3600))
        with mock.patch.object(excel_signin.sys, "platform", "win32"):
            self.assertTrue(excel_signin.ExcelSignIn(reader).enabled)
            with mock.patch.dict(os.environ, {excel_signin.ENV_SWITCH: "0"}):
                self.assertFalse(excel_signin.ExcelSignIn(reader).enabled)
        with mock.patch.object(excel_signin.sys, "platform", "linux"):
            self.assertFalse(excel_signin.ExcelSignIn(reader).enabled)


class KeeperTests(unittest.TestCase):
    def keeper(self, excel: FakeExcel, reader: FakeReader) -> excel_signin.SessionKeeper:
        return excel_signin.SessionKeeper(excel.signin(reader), retry_after=3600)

    def test_a_long_lived_session_is_left_alone(self):
        excel = FakeExcel()
        self.assertFalse(self.keeper(excel, FakeReader(status(5 * 86400))).check_once())
        self.assertEqual(excel.opened, [])

    def test_expiring_soon_refreshes_minimized_then_backs_off(self):
        excel = FakeExcel()
        keeper = self.keeper(excel, FakeReader(status(3 * 3600)))
        self.assertTrue(keeper.check_once())
        self.assertEqual(excel.opened[0][1], True)
        self.assertFalse(keeper.check_once())  # retried at most every retry_after
        self.assertEqual(len(excel.opened), 1)

    def test_missing_session_opens_excel_for_the_user(self):
        excel = FakeExcel()
        keeper = self.keeper(excel, FakeReader(status(0, configured=False), status(10 * 86400)))
        self.assertTrue(keeper.check_once())
        self.assertEqual(excel.opened[0][1], False)


class LauncherSignInTests(unittest.TestCase):
    def test_missing_session_signs_in_before_codex_starts(self):
        reader = FakeReader(status(0, configured=False))
        signin = mock.Mock(enabled=True)
        signin.run.return_value = status(10 * 86400)
        args = mock.Mock(no_auto_signin=False)
        with mock.patch.object(cli.excel_signin, "ExcelSignIn", return_value=signin):
            self.assertTrue(cli._ensure_session(reader, args))
        self.assertEqual(signin.run.call_args.kwargs["interactive"], True)

    def test_without_auto_signin_a_missing_session_stops_the_launcher(self):
        reader = FakeReader(status(0, configured=False))
        args = mock.Mock(no_auto_signin=True)
        self.assertFalse(cli._ensure_session(reader, args))

    def test_a_session_expiring_soon_does_not_delay_codex(self):
        reader = FakeReader(status(3600))
        with mock.patch.object(cli.excel_signin, "ExcelSignIn") as signin:
            self.assertTrue(cli._ensure_session(reader, mock.Mock(no_auto_signin=False)))
        signin.assert_not_called()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from excel_codex_bridge.excel_session_capture import (
    _leveldb_log_records,
    load_windows_excel_session,
)
from excel_codex_bridge.session import SessionReader

from helpers import (
    LEVELDB_BLOCK,
    SESSION_KEY,
    log_file,
    storage_payload,
    webview_db,
    write_batch,
)

KEY = SESSION_KEY
BLOCK = LEVELDB_BLOCK


class LevelDbLogReaderTests(unittest.TestCase):
    def test_log_value_with_latin1_prefix_is_read(self):
        exp = time.time() + 3600
        value = b"\x01" + json.dumps(storage_payload(exp)).encode("latin-1")
        with tempfile.TemporaryDirectory() as directory:
            db = webview_db(Path(directory))
            (db / "000003.log").write_bytes(log_file([write_batch([(KEY, value)])]))
            headers = load_windows_excel_session(Path(directory))
        self.assertEqual(headers["chatgpt-account-id"], "account-id")
        self.assertTrue(headers["authorization"].startswith("Bearer "))

    def test_utf16_value_with_non_latin1_text_is_read(self):
        exp = time.time() + 3600
        payload = storage_payload(exp, name="张三")
        value = b"\x00" + json.dumps(payload, ensure_ascii=False).encode("utf-16-le")
        with tempfile.TemporaryDirectory() as directory:
            db = webview_db(Path(directory))
            (db / "000003.log").write_bytes(log_file([write_batch([(KEY, value)])]))
            headers = load_windows_excel_session(Path(directory))
        self.assertEqual(headers["chatgpt-account-id"], "account-id")

    def test_record_spanning_blocks_is_reassembled(self):
        exp = time.time() + 3600
        padding = b"\x01" + b"x" * 40000
        value = b"\x01" + json.dumps(storage_payload(exp)).encode()
        data = log_file([write_batch([(b"_https://other\x00\x01big", padding)]), write_batch([(KEY, value)])])
        records = list(_leveldb_log_records(data))
        self.assertEqual(len(records), 2)
        self.assertGreater(len(records[0]), BLOCK)

    def test_torn_tail_is_ignored(self):
        exp = time.time() + 3600
        value = b"\x01" + json.dumps(storage_payload(exp)).encode()
        data = log_file([write_batch([(KEY, value)])])
        torn = data + b"\x00\x00\x00\x00\xff\x00\x01partial"
        self.assertEqual(len(list(_leveldb_log_records(torn))), 1)

    def test_latest_expiry_wins_over_older_table_entry(self):
        now = time.time()
        old = json.dumps(storage_payload(now + 600, account="old-account")).encode()
        new = b"\x01" + json.dumps(storage_payload(now + 86400, account="new-account")).encode()
        with tempfile.TemporaryDirectory() as directory:
            db = webview_db(Path(directory))
            (db / "000005.ldb").touch()
            (db / "000006.log").write_bytes(log_file([write_batch([(KEY, new)])]))
            with mock.patch(
                "excel_codex_bridge.excel_session_capture._leveldb_table_entries",
                return_value=[(KEY, old)],
            ):
                headers = load_windows_excel_session(Path(directory))
        self.assertEqual(headers["chatgpt-account-id"], "new-account")

    def test_unreadable_log_does_not_hide_a_table_session(self):
        exp = time.time() + 3600
        value = json.dumps(storage_payload(exp)).encode()
        with tempfile.TemporaryDirectory() as directory:
            db = webview_db(Path(directory))
            (db / "000005.ldb").touch()
            (db / "000006.log").write_bytes(b"\x00\x00\x00\x00\x10\x00\x01short")
            with mock.patch(
                "excel_codex_bridge.excel_session_capture._leveldb_table_entries",
                return_value=[(KEY, value)],
            ):
                headers = load_windows_excel_session(Path(directory))
        self.assertEqual(headers["chatgpt-account-id"], "account-id")


class SessionReaderTests(unittest.TestCase):
    def test_reader_loads_from_explicit_webview_root_and_reports_no_secrets(self):
        exp = time.time() + 7200
        value = b"\x01" + json.dumps(storage_payload(exp)).encode()
        with tempfile.TemporaryDirectory() as directory:
            db = webview_db(Path(directory))
            (db / "000003.log").write_bytes(log_file([write_batch([(KEY, value)])]))
            reader = SessionReader(webview_root=Path(directory))
            status = reader.refresh(force=True)
        self.assertTrue(status["configured"])
        self.assertFalse(status["expired"])
        self.assertGreater(status["expires_in_seconds"], 7000)
        self.assertNotIn("Bearer", json.dumps(status))

    def test_reader_rereads_when_the_cached_token_expired(self):
        with tempfile.TemporaryDirectory() as directory:
            db = webview_db(Path(directory))
            log = db / "000003.log"
            expired = b"\x01" + json.dumps(storage_payload(time.time() - 10, account="a")).encode()
            log.write_bytes(log_file([write_batch([(KEY, expired)])]))
            reader = SessionReader(webview_root=Path(directory))
            self.assertTrue(reader.refresh()["expired"])
            fresh = b"\x01" + json.dumps(storage_payload(time.time() + 3600, account="b")).encode()
            log.write_bytes(log_file([write_batch([(KEY, fresh)])]))
            # No force: an unusable session is re-read immediately.
            self.assertFalse(reader.refresh()["expired"])
        self.assertEqual(reader.store.request_headers(stream=True)["chatgpt-account-id"], "b")

    def test_missing_cache_reports_error(self):
        with tempfile.TemporaryDirectory() as directory:
            status = SessionReader(webview_root=Path(directory)).refresh(force=True)
        self.assertFalse(status["configured"])
        self.assertIn("No signed-in ChatGPT Excel session", status["error"])


if __name__ == "__main__":
    unittest.main()

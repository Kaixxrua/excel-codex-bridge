"""Keeps the in-memory Excel session in step with the add-in's own cache."""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

from . import excel_session_capture, excel_upstream

# Re-read the WebView cache at most this often while the current token is
# usable; an unusable (missing/expired/near-expiry) token is re-read at once.
REFRESH_INTERVAL_SECONDS = 60.0
EXPIRY_MARGIN_SECONDS = 300.0


class SessionReader:
    """Reads the add-in's cached sign-in; never writes the token anywhere.

    ``webview_root`` points the Windows WebView2 reader at an explicit
    ``...\\Microsoft\\Office`` directory, which also works from WSL
    (``/mnt/c/Users/<you>/AppData/Local/Microsoft/Office``).
    """

    def __init__(
        self,
        store: excel_upstream.ExcelSessionStore | None = None,
        *,
        webview_root: Path | None = None,
    ) -> None:
        self.store = store or excel_upstream.ExcelSessionStore(None)
        if webview_root is None and os.environ.get("GHCP_EXCEL_WEBVIEW2_DATA_DIR"):
            webview_root = excel_session_capture._WINDOWS_WEBVIEW_ROOT
        self.webview_root = webview_root
        self._lock = threading.Lock()
        self._last_read: float | None = None
        self.last_error = ""

    @property
    def method(self) -> str | None:
        if self.webview_root is not None or sys.platform == "win32":
            return "webview2-localstorage-leveldb"
        if sys.platform == "darwin":
            return "webkit-localstorage-sqlite"
        return None

    def _usable(self) -> bool:
        status = self.store.status()
        if not status.get("configured"):
            return False
        expires_at = status.get("expires_at")
        return not isinstance(expires_at, (int, float)) or expires_at - time.time() > EXPIRY_MARGIN_SECONDS

    def _load(self) -> dict[str, str]:
        if self.method == "webview2-localstorage-leveldb":
            return excel_session_capture.load_windows_excel_session(self.webview_root)
        return excel_session_capture.load_macos_excel_session()

    def refresh(self, *, force: bool = False) -> dict[str, object]:
        """Load the newest cached session if the current one may be stale."""
        if self.method is None:
            self.last_error = (
                "no Excel session reader on this platform; set GHCP_EXCEL_WEBVIEW2_DATA_DIR "
                "to a Windows ...\\Microsoft\\Office directory"
            )
            return self.status()
        with self._lock:
            fresh = (
                self._last_read is not None
                and time.monotonic() - self._last_read < REFRESH_INTERVAL_SECONDS
            )
            if force or not fresh or not self._usable():
                try:
                    self.store.configure(self._load(), persist=False, allow_expired=True)
                    self.last_error = ""
                except (OSError, RuntimeError, ValueError, UnicodeError) as exc:
                    self.last_error = str(exc)
                self._last_read = time.monotonic()
        return self.status()

    def status(self) -> dict[str, object]:
        """Non-secret session summary."""
        status = self.store.status()
        expires_at = status.get("expires_at")
        remaining = (
            round(expires_at - time.time()) if isinstance(expires_at, (int, float)) else None
        )
        return {
            "configured": bool(status.get("configured")),
            "expired": bool(status.get("expired")),
            "expires_at": expires_at,
            "expires_in_seconds": remaining,
            "reader": self.method or "unavailable",
            "error": self.last_error,
        }

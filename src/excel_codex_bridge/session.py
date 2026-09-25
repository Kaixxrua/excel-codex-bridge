"""Keeps the in-memory session in step with the ChatGPT sign-ins on this computer.

Two sign-ins can serve the Excel backend: the one Codex keeps for itself
(``codex login``, see ``codex_login``) and the one the Excel add-in caches.
By default the Codex one is used, so Excel need not be installed, and the
add-in's is the fallback: when Codex is not signed in with ChatGPT, when its
sign-in has run out, or when the backend refuses it.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path

from . import codex_login, excel_session_capture, excel_upstream

log = logging.getLogger("excel_codex_bridge")

# Re-read the sign-ins at most this often while the current token is
# usable; an unusable (missing/expired/near-expiry) token is re-read at once.
REFRESH_INTERVAL_SECONDS = 60.0
EXPIRY_MARGIN_SECONDS = 300.0

# Which sign-in to use: "auto" (Codex, then Excel), "codex" or "excel".
LOGIN_ENV = "EXCEL_BRIDGE_LOGIN"
LOGINS = ("auto", "codex", "excel")
_ORDER = {"auto": ("codex", "excel"), "codex": ("codex",), "excel": ("excel",)}
NAMES = {"codex": "Codex's ChatGPT sign-in", "excel": "the Excel add-in's ChatGPT session"}


def default_login() -> str:
    value = os.environ.get(LOGIN_ENV, "").strip().lower()
    return value if value in LOGINS else "auto"


def _usable_headers(headers: dict[str, str]) -> bool:
    authorization = headers.get("authorization", "")
    if len(authorization.split(None, 1)) != 2:
        return False
    expires_at = excel_upstream._decode_jwt_exp(authorization)
    return expires_at is None or expires_at - time.time() > EXPIRY_MARGIN_SECONDS


class SessionReader:
    """Reads the cached sign-ins; never writes a token anywhere.

    ``webview_root`` points the Windows WebView2 reader at an explicit
    ``...\\Microsoft\\Office`` directory, which also works from WSL
    (``/mnt/c/Users/<you>/AppData/Local/Microsoft/Office``).  ``login`` picks
    the sign-in, see ``LOGINS``; ``codex_auth`` is Codex's auth.json.
    """

    def __init__(
        self,
        store: excel_upstream.ExcelSessionStore | None = None,
        *,
        webview_root: Path | None = None,
        login: str | None = None,
        codex_auth: Path | None = None,
    ) -> None:
        self.store = store or excel_upstream.ExcelSessionStore(None)
        if webview_root is None and os.environ.get("GHCP_EXCEL_WEBVIEW2_DATA_DIR"):
            webview_root = excel_session_capture._WINDOWS_WEBVIEW_ROOT
        self.webview_root = webview_root
        self.login = login or default_login()
        if self.login not in LOGINS:
            raise ValueError(f"login must be one of: {', '.join(LOGINS)}")
        self.codex_auth = codex_auth or codex_login.auth_path()
        # The sign-in whose headers the store holds: "codex", "excel" or None.
        self.source: str | None = None
        # The auth.json the backend refused, until Codex writes a new one.
        self._codex_refused: tuple[int, int] | None = None
        self._lock = threading.RLock()
        self._last_read: float | None = None
        self.last_error = ""
        self.notes = ""

    @property
    def excel_method(self) -> str | None:
        """How this platform reads the Excel add-in's session, if it can."""
        if self.webview_root is not None or sys.platform == "win32":
            return "webview2-localstorage-leveldb"
        if sys.platform == "darwin":
            return "webkit-localstorage-sqlite"
        return None

    @property
    def method(self) -> str | None:
        if self.source == "codex":
            return codex_login.READER
        if self.source is None and self.login == "codex":
            return codex_login.READER
        return self.excel_method

    def _usable(self) -> bool:
        status = self.store.status()
        if not status.get("configured"):
            return False
        expires_at = status.get("expires_at")
        return not isinstance(expires_at, (int, float)) or expires_at - time.time() > EXPIRY_MARGIN_SECONDS

    def _load(self) -> dict[str, str]:
        """The Excel add-in's cached session."""
        if self.excel_method is None:
            raise RuntimeError(
                "no Excel session reader on this platform; set GHCP_EXCEL_WEBVIEW2_DATA_DIR "
                "to a Windows ...\\Microsoft\\Office directory"
            )
        if self.excel_method == "webview2-localstorage-leveldb":
            return excel_session_capture.load_windows_excel_session(self.webview_root)
        return excel_session_capture.load_macos_excel_session()

    def _load_codex(self) -> dict[str, str]:
        if self._codex_refused is not None:
            if codex_login.signature(self.codex_auth) == self._codex_refused:
                raise ValueError("the Excel backend refused it; sign in again with `codex login` to retry it")
            self._codex_refused = None
        return codex_login.load(self.codex_auth)

    def _read(self, source: str) -> dict[str, str]:
        return self._load_codex() if source == "codex" else self._load()

    def _reload(self) -> None:
        """Put the first usable sign-in in the store, else the first one found."""
        problems: list[str] = []
        chosen = first = None
        for source in _ORDER[self.login]:
            try:
                headers = self._read(source)
            except (OSError, RuntimeError, ValueError, UnicodeError) as exc:
                problems.append(f"{source}: {exc}" if self.login == "auto" else str(exc))
                continue
            if _usable_headers(headers):
                chosen = (source, headers)
                break
            problems.append(f"{source}: expired" if self.login == "auto" else "expired")
            first = first or (source, headers)
        chosen = chosen or first
        # Why a sign-in earlier in the order was passed over.
        self.notes = "; ".join(problems)
        if chosen is None:
            # Keep what the store holds: a file may be mid-rewrite.
            self.last_error = self.notes
            return
        source, headers = chosen
        try:
            self.store.configure(headers, persist=False, allow_expired=True)
        except ValueError as exc:
            self.last_error = f"{source}: {exc}" if self.login == "auto" else str(exc)
            return
        if source != self.source:
            where = f" ({self.codex_auth})" if source == "codex" else ""
            log.info("using %s%s", NAMES[source], where)
        self.source = source
        self.last_error = ""

    def refresh(self, *, force: bool = False) -> dict[str, object]:
        """Load the newest cached sign-in if the current one may be stale."""
        with self._lock:
            fresh = (
                self._last_read is not None
                and time.monotonic() - self._last_read < REFRESH_INTERVAL_SECONDS
            )
            if force or not fresh or not self._usable():
                self._reload()
                self._last_read = time.monotonic()
        return self.status()

    def fall_back(self, refused: str | None) -> bool:
        """After the backend refused the sign-in ``refused``: True when there is another to use.

        Only Codex's sign-in is given up, and only in ``auto``: the add-in's
        session is then used until Codex writes a new auth.json.
        """
        if refused != "codex" or self.login != "auto":
            return False
        with self._lock:
            if self.source == "codex":
                self._codex_refused = codex_login.signature(self.codex_auth)
                self._reload()
                self._last_read = time.monotonic()
                if self.source == "codex":
                    # Nothing else to use; keep trying Codex's sign-in.
                    self._codex_refused = None
                    return False
            return self.source is not None and self._usable()

    def hint(self) -> str:
        """What to do when the sign-in in use is missing or refused."""
        codex = "Run `codex login` and sign in with ChatGPT"
        excel = "open Excel's ChatGPT add-in pane (sign in if asked)"
        if self.login == "codex" or self.source == "codex":
            return f"{codex}, then retry."
        if self.login == "excel" or self.source == "excel":
            return f"{excel[0].upper()}{excel[1:]}, then retry."
        return f"{codex}, or {excel}; then retry."

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
            "login": self.login,
            "source": self.source,
            "codex_auth": str(self.codex_auth),
            "error": self.last_error,
            "notes": self.notes,
        }

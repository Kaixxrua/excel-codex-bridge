"""Let Excel's own ChatGPT add-in sign in, so the user never has to open Excel.

The bridge never signs in by itself.  When the cached add-in session is
missing or close to expiry, it opens a small helper workbook that embeds the
ChatGPT add-in (Microsoft Marketplace asset ``WA200010215``), so Excel opens
the add-in pane: a signed-in pane refreshes its token by itself, a signed-out
one shows the add-in's own sign-in.  Once a fresher session reaches the
add-in's local cache, the helper workbook is closed again.  Windows only.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Callable
from xml.sax.saxutils import escape

from . import codex_config
from .session import EXPIRY_MARGIN_SECONDS, SessionReader

logger = logging.getLogger(__name__)

CHATGPT_ADDIN_ASSET_ID = "WA200010215"
WORKBOOK_NAME = "excel-codex-sign-in.xlsx"
# EXCEL_BRIDGE_AUTO_SIGNIN=0 turns the whole feature off.
ENV_SWITCH = "EXCEL_BRIDGE_AUTO_SIGNIN"
# Ask the add-in for a new token once less than this is left.
REFRESH_AHEAD_SECONDS = 24 * 3600
# How long to wait for the user to sign in, or for a signed-in pane to refresh.
SIGN_IN_TIMEOUT_SECONDS = 600.0
REFRESH_TIMEOUT_SECONDS = 180.0

_SW_SHOWNORMAL = 1
_SW_SHOWMINNOACTIVE = 7
_CREATE_NO_WINDOW = 0x08000000

_NOTES = (
    "excel-codex-bridge：请在右侧 ChatGPT 面板里登录（第一次会提示信任加载项）。登录完成后这个工作簿会自动关闭。",
    "excel-codex-bridge: sign in with the ChatGPT pane on the right (the first time, trust the add-in). "
    "This workbook closes by itself afterwards.",
    "没看到面板？点 开始 → 加载项 → ChatGPT。 / No pane? Home → Add-ins → ChatGPT.",
)


# ─── helper workbook ──────────────────────────────────────────────────────────

_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'


def _cells() -> str:
    return "".join(
        f'<row r="{n}"><c r="A{n}" t="inlineStr"><is><t>{escape(text)}</t></is></c></row>'
        for n, text in enumerate(_NOTES, start=1)
    )


def workbook_parts() -> dict[str, str]:
    """The OOXML parts of a one-sheet workbook with the ChatGPT pane embedded.

    The ``webextension`` + ``taskpanes`` parts follow Microsoft's
    Office-OOXML-EmbedAddin sample: ``visibility="1"`` opens the pane when the
    workbook opens (asking to trust the add-in the first time).
    """
    return {
        "[Content_Types].xml": _XML
        + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '<Override PartName="/xl/webextensions/taskpanes.xml" '
        'ContentType="application/vnd.ms-office.webextensiontaskpanes+xml"/>'
        '<Override PartName="/xl/webextensions/webextension1.xml" '
        'ContentType="application/vnd.ms-office.webextension+xml"/>'
        "</Types>",
        "_rels/.rels": _XML
        + f'<Relationships xmlns="{_REL}">'
        f'<Relationship Id="rId1" Type="{_R}/officeDocument" Target="xl/workbook.xml"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.microsoft.com/office/2011/relationships/webextensiontaskpanes" '
        'Target="xl/webextensions/taskpanes.xml"/>'
        "</Relationships>",
        "xl/workbook.xml": _XML
        + f'<workbook xmlns="{_MAIN}" xmlns:r="{_R}">'
        '<sheets><sheet name="Sign in" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>",
        "xl/_rels/workbook.xml.rels": _XML
        + f'<Relationships xmlns="{_REL}">'
        f'<Relationship Id="rId1" Type="{_R}/worksheet" Target="worksheets/sheet1.xml"/>'
        f'<Relationship Id="rId2" Type="{_R}/styles" Target="styles.xml"/>'
        "</Relationships>",
        "xl/worksheets/sheet1.xml": _XML
        + f'<worksheet xmlns="{_MAIN}"><sheetData>{_cells()}</sheetData></worksheet>',
        "xl/styles.xml": _XML
        + f'<styleSheet xmlns="{_MAIN}">'
        '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="2"><fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        "</styleSheet>",
        "xl/webextensions/taskpanes.xml": _XML
        + '<wetp:taskpanes xmlns:wetp="http://schemas.microsoft.com/office/webextensions/taskpanes/2010/11">'
        '<wetp:taskpane dockstate="right" visibility="1" width="350" row="4">'
        f'<wetp:webextensionref xmlns:r="{_R}" r:id="rId1"/>'
        "</wetp:taskpane></wetp:taskpanes>",
        "xl/webextensions/_rels/taskpanes.xml.rels": _XML
        + f'<Relationships xmlns="{_REL}">'
        '<Relationship Id="rId1" Type="http://schemas.microsoft.com/office/2011/relationships/webextension" '
        'Target="webextension1.xml"/>'
        "</Relationships>",
        "xl/webextensions/webextension1.xml": _XML
        + '<we:webextension xmlns:we="http://schemas.microsoft.com/office/webextensions/webextension/2010/11" '
        'id="{6F2B4F3A-1C84-4E36-9D0B-5A7C2E9B41D7}">'
        f'<we:reference id="{CHATGPT_ADDIN_ASSET_ID}" version="1.0.0.0" store="en-US" storeType="OMEX"/>'
        "<we:alternateReferences/>"
        '<we:properties><we:property name="Office.AutoShowTaskpaneWithDocument" value="true"/></we:properties>'
        "<we:bindings/>"
        f'<we:snapshot xmlns:r="{_R}"/>'
        "</we:webextension>",
    }


def write_workbook(directory: Path) -> Path:
    """Write the helper workbook, leaving an identical (maybe open) copy alone."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / WORKBOOK_NAME
    tmp = directory / f".{WORKBOOK_NAME}.tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in workbook_parts().items():
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content.encode("utf-8"))
    try:
        if path.exists() and path.read_bytes() == tmp.read_bytes():
            tmp.unlink()
            return path
        os.replace(tmp, path)
    except OSError:
        # Excel holds the old copy open; it embeds the same pane.
        tmp.unlink(missing_ok=True)
        if not path.exists():
            raise
    return path


# ─── Excel process control (Windows) ──────────────────────────────────────────

# Windows PowerShell 5.1 ships with every supported Windows and still has
# Marshal.GetActiveObject.  Only the helper workbook is closed; Excel quits
# only if the bridge started it and nothing else is open.
_CLOSE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
try { $app = [Runtime.InteropServices.Marshal]::GetActiveObject('Excel.Application') } catch { exit 3 }
$closed = $false
foreach ($wb in @($app.Workbooks)) {
  if ([string]::Equals($wb.FullName, $env:EXCEL_CODEX_WORKBOOK, [StringComparison]::OrdinalIgnoreCase)) {
    $wb.Close($false); $closed = $true
  }
}
if ($closed -and $env:EXCEL_CODEX_QUIT -eq '1' -and $app.Workbooks.Count -eq 0) { $app.Quit() }
if ($closed) { exit 0 } else { exit 4 }
"""


def _excel_running() -> bool:
    try:
        output = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq EXCEL.EXE", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, timeout=15, creationflags=_CREATE_NO_WINDOW,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return True  # unknown: never quit the user's Excel
    return "excel.exe" in output.lower()


def _open_in_excel(path: Path, *, minimized: bool) -> None:
    os.startfile(str(path), "open", "", None, _SW_SHOWMINNOACTIVE if minimized else _SW_SHOWNORMAL)


def _close_in_excel(path: Path, *, quit_excel: bool) -> bool:
    env = dict(os.environ, EXCEL_CODEX_WORKBOOK=str(path), EXCEL_CODEX_QUIT="1" if quit_excel else "0")
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-Command", _CLOSE_SCRIPT],
            env=env, capture_output=True, timeout=60, creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


# ─── sign-in flow ─────────────────────────────────────────────────────────────

def expiry(status: dict) -> float | None:
    value = status.get("expires_at")
    return float(value) if isinstance(value, (int, float)) else None


def usable(status: dict) -> bool:
    if not status.get("configured") or status.get("expired"):
        return False
    value = expiry(status)
    return value is None or value - time.time() > EXPIRY_MARGIN_SECONDS


def needs_refresh(status: dict) -> bool:
    if not usable(status):
        return True
    value = expiry(status)
    return value is not None and value - time.time() < REFRESH_AHEAD_SECONDS


def _fresh(status: dict, previous_expiry: float | None) -> bool:
    if not usable(status):
        return False
    value = expiry(status)
    return previous_expiry is None or (value is not None and value > previous_expiry)


def enabled_by_default(reader: SessionReader) -> bool:
    return (
        sys.platform == "win32"
        and reader.method == "webview2-localstorage-leveldb"
        and os.environ.get(ENV_SWITCH, "1").strip().lower() not in {"0", "false", "no", "off"}
    )


class ExcelSignIn:
    """Opens the add-in pane in Excel and waits for a fresher cached session."""

    def __init__(
        self,
        reader: SessionReader,
        *,
        enabled: bool | None = None,
        workbook_dir: Path | None = None,
        opener: Callable[..., None] | None = None,
        closer: Callable[..., bool] | None = None,
        excel_running: Callable[[], bool] | None = None,
        poll_seconds: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.reader = reader
        self.enabled = enabled_by_default(reader) if enabled is None else enabled
        self.workbook_dir = workbook_dir
        self._open = opener or _open_in_excel
        self._close = closer or _close_in_excel
        self._excel_running = excel_running or _excel_running
        self.poll_seconds = poll_seconds
        self._sleep = sleep
        self.clock = clock
        self._lock = threading.Lock()

    def run(self, *, interactive: bool, timeout: float, previous_expiry: float | None = None) -> dict | None:
        """Open the pane and wait up to ``timeout`` s; the fresh status, or None.

        ``interactive`` shows Excel normally so the user can sign in; otherwise
        Excel starts minimized for a signed-in pane to refresh on its own.
        Raises OSError when Excel cannot be started.
        """
        with self._lock:
            was_running = self._excel_running()
            path = write_workbook(self.workbook_dir or codex_config.state_dir())
            self._open(path, minimized=not interactive)
            try:
                deadline = self.clock() + timeout
                while True:
                    status = self.reader.refresh(force=True)
                    if _fresh(status, previous_expiry):
                        return status
                    if self.clock() >= deadline:
                        return None
                    self._sleep(self.poll_seconds)
            finally:
                if not self._close(path, quit_excel=not was_running):
                    logger.info("could not close the helper workbook %s", path)


class SessionKeeper:
    """Background refresh for a long-running bridge (desktop / serve / launcher)."""

    def __init__(
        self,
        signin: ExcelSignIn,
        *,
        first_delay: float = 5.0,
        check_every: float = 300.0,
        retry_after: float = 3 * 3600.0,
    ) -> None:
        self.signin = signin
        self.first_delay = first_delay
        self.check_every = check_every
        self.retry_after = retry_after
        self._last_attempt: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.signin.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="excel-session-keeper", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        delay = self.first_delay
        while not self._stop.wait(delay):
            delay = self.check_every
            try:
                self.check_once()
            except Exception:  # keep serving whatever happens here
                logger.exception("automatic Excel sign-in failed")

    def check_once(self) -> bool:
        """Refresh through Excel if due; True when an attempt was made."""
        status = self.signin.reader.refresh(force=True)
        if not needs_refresh(status):
            return False
        now = self.signin.clock()
        if self._last_attempt is not None and now - self._last_attempt < self.retry_after:
            return False
        self._last_attempt = now
        signed_in = usable(status)
        logger.info(
            "Excel session %s; opening the ChatGPT pane in Excel",
            "expires soon" if signed_in else "is missing or expired",
        )
        fresh = self.signin.run(
            interactive=not signed_in,
            timeout=REFRESH_TIMEOUT_SECONDS if signed_in else SIGN_IN_TIMEOUT_SECONDS,
            previous_expiry=expiry(status),
        )
        logger.info("Excel session %s", "refreshed" if fresh else "not refreshed yet")
        return True

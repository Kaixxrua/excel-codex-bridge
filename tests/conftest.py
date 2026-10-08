import os
import tempfile
from pathlib import Path

# Tests never ask GitHub for the latest release; test_updates.py turns it on where needed.
os.environ["EXCEL_BRIDGE_UPDATE_CHECK"] = "0"
# Existing tests exercise the legacy Excel adapter explicitly. Native route
# tests select codex and mock its fixed upstream; no test uses a real login.
os.environ["EXCEL_BRIDGE_ROUTE"] = "excel"
# Nor read the Codex sign-in of whoever runs them; test_codex_login.py writes its own.
os.environ["EXCEL_BRIDGE_CODEX_AUTH"] = str(Path(tempfile.mkdtemp(prefix="no-codex-login-")) / "auth.json")
os.environ.pop("EXCEL_BRIDGE_LOGIN", None)
# Nor look up where this machine's proxy exit is; test_exit_timezone.py turns it on where needed.
os.environ["EXCEL_BRIDGE_TIMEZONE"] = "off"
# Nor wait for the backend to come back; test_reconnect.py turns it on where needed.
os.environ["EXCEL_BRIDGE_CONNECT_WAIT"] = "0"
# Nor draw with an image model chosen on this machine; test_image_generation.py sets its own.
os.environ.pop("EXCEL_BRIDGE_IMAGE_MODEL", None)
# Nor PING the backend at an interval chosen on this machine; test_upstream_ping.py sets its own.
os.environ.pop("EXCEL_BRIDGE_UPSTREAM_PING", None)


def _workflow_escape(text: str, *, prop: bool = False) -> str:
    text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return text.replace(":", "%3A").replace(",", "%2C") if prop else text


def pytest_runtest_logreport(report):
    # On GitHub Actions a failure also shows as an annotation on the commit page,
    # which anyone can read, unlike the job log.
    if report.failed and os.environ.get("GITHUB_ACTIONS") == "true":
        title = _workflow_escape(f"{report.nodeid} ({report.when})", prop=True)
        print(f"\n::error title={title}::{_workflow_escape(report.longreprtext[-3000:])}", flush=True)

import os
import tempfile
from pathlib import Path

# Tests never ask GitHub for the latest release; test_updates.py turns it on where needed.
os.environ["EXCEL_BRIDGE_UPDATE_CHECK"] = "0"
# Nor read the Codex sign-in of whoever runs them; test_codex_login.py writes its own.
os.environ["EXCEL_BRIDGE_CODEX_AUTH"] = str(Path(tempfile.mkdtemp(prefix="no-codex-login-")) / "auth.json")
os.environ.pop("EXCEL_BRIDGE_LOGIN", None)

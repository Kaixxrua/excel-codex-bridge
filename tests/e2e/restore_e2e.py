"""End-to-end: the double-click restore launcher puts Codex back on its own setup.

    python tests/e2e/restore_e2e.py ./excel-codex-restore.command
    python tests/e2e/restore_e2e.py <package>/excel-codex-restore.cmd

Runs the launcher the way a double-click does (nothing to read on stdin, so a
closing `pause` returns at once) against a throwaway CODEX_HOME. Its
config.toml is what a desktop window that was killed leaves behind, around a
`print-config` snippet the user had pasted in by hand earlier (as `desktop`
writes it: `enable` in desktop_config.py): both have to go, and only they.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

LEFT_BEHIND = """\
# >>> excel-codex-bridge: added by `excel-codex desktop`; `excel-codex desktop --off` removes it
model_provider = 'openai'
openai_base_url = 'http://127.0.0.1:8765/v1'
model = 'gpt-6-sol'
model_catalog_json = '{catalog}'
features.apps = false
features.remote_plugin = false
# <<< excel-codex-bridge

# my own settings
# excel-codex-bridge disabled: model = "gpt-5.5"
approval_policy = "on-request"

# excel-codex-bridge disabled: [model_providers.excel-bridge]
# excel-codex-bridge disabled: name = "old paste from print-config"
# excel-codex-bridge disabled: base_url = "http://127.0.0.1:9999/v1"
# excel-codex-bridge disabled: {blank}
[history]
persistence = "none"

# >>> excel-codex-bridge provider
[model_providers.excel-bridge]
name = 'Excel Bridge'
base_url = 'http://127.0.0.1:8765/v1'
wire_api = "responses"
http_headers = {{ "x-openai-actor-authorization" = "excel-codex-bridge" }}
# <<< excel-codex-bridge provider
"""

OWN_SETUP = """\
# my own settings
model = "gpt-5.5"
approval_policy = "on-request"

# excel-codex restore took out: [model_providers.excel-bridge]
# excel-codex restore took out: name = "old paste from print-config"
# excel-codex restore took out: base_url = "http://127.0.0.1:9999/v1"

[history]
persistence = "none"
"""


def annotate(message: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        text = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error title=restore e2e::{text}", flush=True)


def fail(message: str, output: str = "") -> None:
    print(output)
    annotate(f"{message}\n{output[-6000:]}")
    raise SystemExit(f"FAIL: {message}")


def main() -> None:
    launcher = Path(sys.argv[1]).resolve()
    root = Path(tempfile.mkdtemp(prefix="restore-e2e-"))
    home, bridge = root / "codex-home", root / "bridge-home"
    home.mkdir()
    config = home / "config.toml"
    catalog = bridge / "codex-model-catalog.json"
    config.write_bytes(LEFT_BEHIND.format(catalog=catalog, blank="").encode("utf-8"))
    env = dict(os.environ, CODEX_HOME=str(home), EXCEL_BRIDGE_HOME=str(bridge),
               EXCEL_BRIDGE_ASSUME_CODEX_QUIT="1")
    if launcher.suffix.lower() == ".cmd":
        command: str | list[str] = f'cmd.exe /d /s /c ""{launcher}""'
    else:
        command = [str(launcher)]
    result = subprocess.run(command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=600)
    output = result.stdout.decode("utf-8", "replace")
    print(output)
    if result.returncode != 0:
        fail(f"{launcher.name} exited with {result.returncode}", output)
    for text in ("Codex is back on its own setup", "`excel-codex desktop` had put in",
                 "[model_providers.excel-bridge] (the whole table)", "Now fully quit the Codex desktop app"):
        if text not in output:
            fail(f"expected {text!r} in the output", output)
    restored = config.read_bytes().decode("utf-8").replace("\r\n", "\n")
    if restored != OWN_SETUP:
        fail("config.toml is not back on Codex's own setup", restored)
    backup = config.with_name("config.toml.before-excel-codex-restore")
    if backup.read_bytes() != LEFT_BEHIND.format(catalog=catalog, blank="").encode("utf-8"):
        fail("the file as it was is not saved next to it")
    print(f"PASS: {launcher.name} put config.toml back on Codex's own setup")


if __name__ == "__main__":
    main()

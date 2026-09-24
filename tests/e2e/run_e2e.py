"""End-to-end check: a launcher and the real Codex CLI against a fake Excel backend.

    python tests/e2e/run_e2e.py [--model MODEL] -- <launcher command...>

The launcher is ``dist/excel-codex/excel-codex.exe``, ``excel-codex.cmd`` or
``./excel-codex.sh``. Codex must be on PATH, and this Python needs the
project requirements (the fake backend runs in-process on FastAPI).

The fake backend answers the first request with a ``run_officejs`` call that
wraps Codex's shell tool, and the second with text saying whether the tool's
output came back.  That covers session reading, the ``-c`` overrides, the tool
call round trip and the environment Codex hands to its commands.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse, StreamingResponse  # noqa: E402

from helpers import write_webview_session  # noqa: E402

USER_PYTHONPATH = "e2e-user-pythonpath"
PYTHON = "python" if sys.platform == "win32" else "python3"
# Prints a marker only a real execution can produce, plus the PYTHONPATH Codex
# gave the command.  Works in bash, PowerShell and cmd alike.
PROBE = (
    f"{PYTHON} -c \"import os; print('bridge-e2e-' + str(6*7), "
    "'PP=[' + os.environ.get('PYTHONPATH', '') + ']')\""
)
USAGE = {
    "input_tokens": 120,
    "input_tokens_details": {"cached_tokens": 0},
    "output_tokens": 12,
    "output_tokens_details": {"reasoning_tokens": 0},
    "total_tokens": 132,
}


def sse(event: str, payload: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode()


def shell_call(raw_request: str) -> tuple[str, dict]:
    """Call whichever shell tool this Codex build declared."""
    if "exec_command" in raw_request:
        return "exec_command", {"cmd": PROBE}
    if "shell_command" in raw_request:
        return "shell_command", {"command": PROBE}
    return "shell", {"command": ["bash", "-lc", PROBE] if sys.platform != "win32" else ["cmd", "/c", PROBE]}


class FakeExcelBackend:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.unexpected: list[str] = []
        self.shell_tool = ""
        counter = itertools.count(1)
        app = FastAPI()

        @app.post("/basispoints/api/responses")
        async def responses(request: Request):
            n = next(counter)
            body = await request.json()
            self.requests.append(body)
            raw = json.dumps(body)
            if n == 1:
                name, arguments = shell_call(raw)
                self.shell_tool = name
                code = json.dumps({"name": name, "arguments": arguments})
                call_args = json.dumps({"code": code})
                item = {
                    "type": "function_call", "id": "fc_1", "call_id": "call_1",
                    "name": "run_officejs", "arguments": call_args, "status": "completed",
                }
                events = [
                    sse("response.created", {"type": "response.created",
                        "response": {"id": "resp_1", "status": "in_progress", "output": []}}),
                    sse("response.output_item.added", {"type": "response.output_item.added", "output_index": 0,
                        "item": {**item, "arguments": "", "status": "in_progress"}}),
                    sse("response.function_call_arguments.delta", {"type": "response.function_call_arguments.delta",
                        "output_index": 0, "item_id": "fc_1", "delta": call_args}),
                    sse("response.function_call_arguments.done", {"type": "response.function_call_arguments.done",
                        "output_index": 0, "item_id": "fc_1", "arguments": call_args}),
                    sse("response.output_item.done", {"type": "response.output_item.done",
                        "output_index": 0, "item": item}),
                    sse("response.completed", {"type": "response.completed", "response": {
                        "id": "resp_1", "status": "completed", "model": body.get("model"),
                        "output": [item], "usage": USAGE}}),
                ]
            else:
                seen = "bridge-e2e-42" in raw
                text = "done: tool output seen" if seen else "done: tool output MISSING"
                msg = {"type": "message", "id": f"msg_{n}", "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text", "text": text, "annotations": []}]}
                events = [
                    sse("response.created", {"type": "response.created",
                        "response": {"id": f"resp_{n}", "status": "in_progress", "output": []}}),
                    sse("response.output_item.added", {"type": "response.output_item.added", "output_index": 0,
                        "item": {**msg, "content": [], "status": "in_progress"}}),
                    sse("response.output_text.delta", {"type": "response.output_text.delta", "output_index": 0,
                        "item_id": msg["id"], "content_index": 0, "delta": text}),
                    sse("response.output_text.done", {"type": "response.output_text.done", "output_index": 0,
                        "item_id": msg["id"], "content_index": 0, "text": text}),
                    sse("response.output_item.done", {"type": "response.output_item.done",
                        "output_index": 0, "item": msg}),
                    sse("response.completed", {"type": "response.completed", "response": {
                        "id": f"resp_{n}", "status": "completed", "model": body.get("model"),
                        "output": [msg], "usage": USAGE}}),
                ]

            async def stream():
                for event in events:
                    yield event

            return StreamingResponse(stream(), media_type="text/event-stream")

        @app.api_route("/{path:path}", methods=["GET", "POST"])
        async def other(path: str):
            self.unexpected.append(path)
            return JSONResponse({"error": {"message": "not here"}}, status_code=404)

        self.app = app


def start_server(app) -> tuple[uvicorn.Server, int]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline:
            raise SystemExit("fake backend did not start")
        time.sleep(0.05)
    return server, port


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default="gpt-5.6-sol-excel")
    parser.add_argument("--timeout", type=int, default=900, help="seconds for the whole launcher run")
    parser.add_argument("launcher", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    launcher = args.launcher[1:] if args.launcher[:1] == ["--"] else args.launcher
    if not launcher:
        parser.error("give the launcher command after --")
    if Path(launcher[0]).exists():
        launcher[0] = str(Path(launcher[0]).resolve())

    root = Path(tempfile.mkdtemp(prefix="excel-codex-e2e-"))
    webview = write_webview_session(root / "webview", time.time() + 3 * 86400, account="e2e-account")
    project = root / "project"
    project.mkdir()
    backend = FakeExcelBackend()
    server, port = start_server(backend.app)

    env = dict(os.environ)
    env.update(
        CODEX_HOME=str(root / "codex-home"),
        EXCEL_BRIDGE_HOME=str(root / "bridge-home"),
        GHCP_EXCEL_RESPONSES_URL=f"http://127.0.0.1:{port}/basispoints/api/responses",
        PYTHONPATH=USER_PYTHONPATH,
    )
    Path(env["CODEX_HOME"]).mkdir()
    command = [
        *launcher, "--webview-dir", str(webview), "--model", args.model,
        "--", "exec", "--skip-git-repo-check", "-s", "danger-full-access",
        # A subcommand -c must not knock out the bridge's own overrides.
        "-c", "model_reasoning_effort=high",
        "Run the check.",
    ]
    print("$", subprocess.list2cmdline(command), flush=True)
    started = time.monotonic()
    result = subprocess.run(
        command, cwd=project, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        timeout=args.timeout, text=True, encoding="utf-8", errors="replace",
    )
    server.should_exit = True
    output = result.stdout

    upstream_model = args.model.removesuffix("-excel")
    checks = [
        (result.returncode == 0, f"launcher exit code {result.returncode}"),
        ("provider: excel-bridge" in output, "Codex did not use the excel-bridge provider"),
        ("done: tool output seen" in output, "the tool output did not reach the model"),
        (len(backend.requests) >= 2, f"expected 2+ upstream requests, got {len(backend.requests)}"),
        (all(r.get("model") == upstream_model for r in backend.requests),
         f"upstream model is not {upstream_model}"),
        (all(r.get("reasoning_effort") == "high" for r in backend.requests),
         "reasoning effort from the subcommand -c did not arrive"),
        (len(backend.requests) >= 2 and f"PP=[{USER_PYTHONPATH}]" in json.dumps(backend.requests[1]),
         "Codex's commands saw a different PYTHONPATH"),
        (not backend.unexpected, f"unexpected upstream paths: {backend.unexpected}"),
    ]
    failures = [message for ok, message in checks if not ok]

    print(output)
    print(f"--- {len(backend.requests)} upstream request(s), shell tool {backend.shell_tool or '-'}, "
          f"{time.monotonic() - started:.0f}s")
    for body in backend.requests:
        print("   ", body.get("model"), "reasoning_effort=", body.get("reasoning_effort"))
    if failures:
        log = root / "bridge-home" / "bridge.log"
        if log.exists():
            print("--- bridge.log\n" + log.read_text(encoding="utf-8", errors="replace")[-4000:])
        if len(backend.requests) >= 2:
            tail = json.dumps(backend.requests[1])
            print("--- 2nd request tail\n" + tail[-1500:])
        for message in failures:
            print("FAIL:", message)
        return 1
    shutil.rmtree(root, ignore_errors=True)
    print("e2e ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

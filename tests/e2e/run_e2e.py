"""End-to-end check: a launcher and the real Codex CLI against a fake Excel backend.

    python tests/e2e/run_e2e.py [--model MODEL] [--desktop] -- <launcher command...>

The launcher is ``dist/excel-codex/excel-codex.exe``, ``excel-codex.cmd`` or
``./excel-codex.sh``. Codex must be on PATH, and this Python needs the
project requirements (the fake backend runs in-process on FastAPI).

The fake backend answers the first request with a ``run_officejs`` call that
wraps Codex's shell tool, and the second with text saying whether the tool's
output came back.  That covers session reading, the ``-c`` overrides, the tool
call round trip and the environment Codex hands to its commands.

``--desktop`` runs ``<launcher> desktop`` instead and a plain ``codex exec``
next to it, the way the desktop app picks up ``config.toml``; the user's own
config must come back byte for byte when the desktop window stops.

``--parallel`` makes the first answer two independent ``run_officejs`` calls
at once: Codex must run both, and the bridge must replay both native calls
followed by both results in the next request.

``--imagegen`` makes the first answer a call to Codex's image tool
(``image_gen.imagegen``): Codex must send it to the bridge's
``images/generations``, the bridge must draw it with the add-in's image
endpoint, and the picture must come back to the model in the tool result.

``--codex-login`` also leaves a ChatGPT sign-in where ``codex login`` puts it:
the bridge must use it rather than the Excel session.  With
``--codex-login refused`` the backend refuses it, and the bridge must retry
with the Excel session and keep that one.

``--shared`` signs Codex itself in (a made-up API key in its ``auth.json``):
the bridge must then stand in for Codex's own ``openai`` provider, so that
the conversation is filed under ``openai`` with the model's official name,
the way the official sign-in files it.  Codex tries a WebSocket first there;
the bridge's 426 must send it to HTTP.

``--migrate`` starts a conversation the 0.5.3 way, filed under the bridge's own
``excel-bridge`` provider, then signs Codex in and moves it into the shared
list with ``threads migrate`` (``--migrate desktop``: by starting ``desktop``,
which moves it by itself).  A plain ``codex exec resume --last`` then carries
it on with only Codex's own providers, the way the desktop app has them with
the bridge off; its ``openai`` provider reaches the bridge through ``serve``.
``--migrate relay`` starts it through a relay instead, set up the way a relay's
Codex config template sets it up: a provider of its own named ``OpenAI`` (not
Codex's ``openai``: names are case-sensitive), with ``serve`` in the relay's
place.  ``threads migrate --from OpenAI`` then moves it under ``openai``.

``--images`` also attaches a picture (``codex exec -i``).  Like the real
backend, the fake one refuses a user message with an inline picture; the
bridge must then upload it once to the attachments endpoint, the way the
add-in does, and name it by its file id from then on.

``--network-drop`` cuts every connection to the backend for a while after the
first, the way a proxy node that went away does: the bridge must keep trying,
and Codex must see an answer, not the drop.

``--subagent`` turns on Codex's ``multi_agent_v2`` and has the first answer
also spawn a helper agent (``collaboration.spawn_agent``).  The helper must be
given the task as text: labelled encrypted, the backend could not read it.

``--ultra`` does the same with Codex's ``ultra`` effort instead of the
feature switch: the bridge's model entries must turn on multi-agent v2 and
Codex's unasked delegation by themselves, and the backend must be asked for
xhigh, the deepest it has (it refuses ``max`` and ``ultra``).

``--tool-search`` gives Codex an MCP server (a fake one, next to the backend):
with the bridge's model entries Codex keeps its tools out of the request and
offers ``tool_search`` instead.  The first answer searches, the second calls
the tool the search loaded, and its output must reach the model; the tool
must never join the catalog at the top of the prompt.

``--lite`` runs Codex through a relay the way a relay's config template sets it
up, with ``serve`` in the relay's place and without the bridge's model
entries: Codex's own for this model then send the tools in an input item
(Responses Lite), and only code mode's ``exec`` and ``wait`` to the model.
The first answer calls ``exec`` with JavaScript that runs the shell tool
through ``tools``; the bridge must read the tools from that item.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import datetime as dt
import itertools
import json
import os
import queue
import shutil
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import re
import shlex
import tempfile
import threading
import time
import traceback
import urllib.request
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse  # noqa: E402

from excel_codex_bridge import codex_config, excel_upstream, exit_timezone  # noqa: E402
from helpers import write_codex_login, write_webview_session  # noqa: E402

USER_PYTHONPATH = "e2e-user-pythonpath"
USER_CONFIG = '# e2e user config\nmodel = "gpt-5.5"\n\n[history]\npersistence = "none"\n'
CODEX_ARGS = [
    "exec", "--skip-git-repo-check", "-s", "danger-full-access",
    # A subcommand -c must not knock out the bridge's own overrides.
    "-c", "model_reasoning_effort=high",
    "Run the check.",
]
# Carries the conversation above on (Codex lists only its current provider's).
RESUME_ARGS = [*CODEX_ARGS[:-1], "resume", "--last", "Carry on."]
# What a relay's Codex config template may call its provider.
RELAY_PROVIDER = "OpenAI"
PYTHON = "python" if sys.platform == "win32" else "python3"
# Prints a marker only a real execution can produce, plus the PYTHONPATH Codex
# gave the command.  Works in bash, PowerShell and cmd alike.
def probe(expression: str) -> str:
    return (
        f"{PYTHON} -c \"import os; print('bridge-e2e-' + str({expression}), "
        "'PP=[' + os.environ.get('PYTHONPATH', '') + ']')\""
    )


PROBE = probe("6*7")  # bridge-e2e-42
SECOND_PROBE = probe("6*7+1")  # bridge-e2e-43
USAGE = {
    "input_tokens": 120,
    "input_tokens_details": {"cached_tokens": 0},
    "output_tokens": 12,
    "output_tokens_details": {"reasoning_tokens": 0},
    "total_tokens": 132,
}


def sse(event: str, payload: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode()


def png(width: int = 16, height: int = 16) -> bytes:
    """A small real PNG (Codex decodes pictures before sending them)."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (len(data).to_bytes(4, "big") + kind + data
                + zlib.crc32(kind + data).to_bytes(4, "big"))

    rows = b"".join(b"\x00" + b"".join(bytes((x * 15, y * 15, 128)) for x in range(width)) for y in range(height))
    header = width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes((8, 2, 0, 0, 0))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows))
            + chunk(b"IEND", b""))


def pictures(value) -> list[dict]:
    """Every input_image anywhere in a request body."""
    if isinstance(value, list):
        return [part for item in value for part in pictures(item)]
    if isinstance(value, dict):
        own = [value] if value.get("type") == "input_image" else []
        return own + [part for item in value.values() for part in pictures(item)]
    return []


def inline_in_user_message(body: dict) -> bool:
    return any(item.get("type", "message") == "message" and "data:image" in json.dumps(item)
               for item in body.get("input", []) if isinstance(item, dict))


def shell_call(raw_request: str, command: str = PROBE) -> tuple[str, dict]:
    """Call whichever shell tool this Codex build declared."""
    if "exec_command" in raw_request:
        return "exec_command", {"cmd": command}
    if "shell_command" in raw_request:
        return "shell_command", {"command": command}
    return "shell", {"command": ["bash", "-lc", command] if sys.platform != "win32" else ["cmd", "/c", command]}


def transport_call(n: int, name: str, arguments: dict | str) -> dict:
    """A run_officejs call wrapping ``name``: a function tool's ``arguments``, or a custom tool's raw input."""
    code = json.dumps({"name": name, "input": arguments} if isinstance(arguments, str)
                      else {"name": name, "arguments": arguments})
    return {
        "type": "function_call", "id": f"fc_{n}", "call_id": f"call_{n}",
        "name": "run_officejs", "arguments": json.dumps({"code": code}), "status": "completed",
    }


IMAGE_PROMPT = "a blue whale in a spreadsheet"
# --tool-search: the MCP server Codex is given, its one tool, and what the tool answers.
MCP_SERVER = "e2e_fake"
MCP_TOOL = "echo_probe"
MCP_ECHO = "mcp-e2e-BRIDGE-E2E-42"
FAKE_MCP = f"""import json, sys
TOOL = {{"name": {MCP_TOOL!r}, "description": "Echo probe for the bridge e2e: says the text back.",
        "inputSchema": {{"type": "object", "properties": {{"text": {{"type": "string"}}}}, "required": ["text"]}},
        "annotations": {{"readOnlyHint": True}}}}
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    method = message.get("method")
    if method == "initialize":
        result = {{"protocolVersion": message["params"].get("protocolVersion", "2025-06-18"),
                  "capabilities": {{"tools": {{}}}}, "serverInfo": {{"name": {MCP_SERVER!r}, "version": "1"}}}}
    elif method == "tools/list":
        result = {{"tools": [TOOL]}}
    elif method == "tools/call":
        text = message["params"].get("arguments", {{}}).get("text", "")
        result = {{"content": [{{"type": "text", "text": "mcp-e2e-" + text.upper()}}]}}
    else:
        result = {{}}
    sys.stdout.write(json.dumps({{"jsonrpc": "2.0", "id": message["id"], "result": result}}) + "\\n")
    sys.stdout.flush()
"""
# --subagent: what the model asks the helper it spawns to do.
SUBAGENT_TASK = "Count the rows of the sheet and say subagent-task-e2e."
EXIT_IP = "1.1.1.1"


def code_mode_call(raw_request: str, command: str = PROBE) -> tuple[str, str]:
    """Code mode's ``exec``, running whichever shell tool this model has through ``tools``."""
    # exec's own description names exec_command as an example; it declares the tools it has.
    if "{ shell_command(" in raw_request:
        call = f"tools.shell_command({{command: {json.dumps(command)}}})"
    else:
        call = f"tools.exec_command({{cmd: {json.dumps(command)}}})"
    return "exec", f"const result = await {call};\ntext(result);\n"


def exit_zone_for(now: dt.datetime) -> str:
    """A proxy exit whose day is not today in UTC, so moving the date shows."""
    return "Pacific/Kiritimati" if now.hour >= 10 else "Pacific/Pago_Pago"


class FakeExcelBackend:
    def __init__(self, parallel: bool = False, imagegen: bool = False, refuse: str | None = None,
                 exit_zone: str = "Pacific/Kiritimati", rate_limited: int = 0,
                 rate_limited_for: float = 0.0, subagent: bool = False, lite: bool = False,
                 tool_search: bool = False) -> None:
        self.requests: list[dict] = []
        # With subagent: the helper's own requests, kept apart since they come alongside the rest.
        self.helper_requests: list[dict] = []
        helper_asked = asyncio.Event()
        # Requests failed on the shared tokens-per-minute limit, before any other is answered:
        # the first ``rate_limited``, and all in the first ``rate_limited_for`` seconds.
        self.limited: list[dict] = []
        self.limited_until: float | None = None
        # The proxy exit the bridge finds: this IP, in this timezone.
        self.exit_zone = exit_zone
        self.lookups: list[str] = []
        # The account of every request, and the one this backend refuses to serve.
        self.accounts: list[str] = []
        self.refuse = refuse
        # What the bridge sent to the add-in's image endpoint, and the picture drawn.
        self.drawings: list[tuple[dict, dict]] = []
        self.drawn = base64.b64encode(png(12, 8)).decode()
        self.unexpected: list[str] = []
        self.shell_tool = ""
        # Requests refused for an inline picture in a user message, and uploads.
        self.refused: list[dict] = []
        self.uploads: list[tuple[dict, bytes]] = []
        counter = itertools.count(1)
        app = FastAPI()

        @app.post("/basispoints/api/attachments")
        async def attachments(request: Request):
            self.uploads.append((dict(request.headers), await request.body()))
            return JSONResponse({"openai_file_id": f"file-e2e-{len(self.uploads)}", "filename": "picture.png",
                                 "content_type": "image/png", "size": 1, "input_tokens": 85})

        @app.post("/basispoints/api/images/generations")
        async def generations(request: Request):
            self.drawings.append((dict(request.headers), await request.json()))
            return JSONResponse({"created": 1, "background": "opaque", "output_format": "png",
                                 "data": [{"b64_json": self.drawn}]})

        @app.get("/cdn-cgi/trace")
        async def trace():
            return PlainTextResponse(f"fl=1\nh=127.0.0.1\nip={EXIT_IP}\nts=1\n")

        @app.get("/geo/{ip}")
        async def geo(ip: str):
            self.lookups.append(ip)
            # No offset: the day there must come from the bundled timezone data.
            return JSONResponse({"ip": ip, "timezone": self.exit_zone})

        @app.middleware("http")
        async def check_sign_in(request: Request, call_next):
            if not request.url.path.startswith("/basispoints/"):
                return await call_next(request)
            account = request.headers.get("chatgpt-account-id", "")
            self.accounts.append(account)
            if account == self.refuse:
                return JSONResponse({"error": {"message": "not for this client"}}, status_code=401)
            return await call_next(request)

        @app.post("/basispoints/api/responses")
        async def responses(request: Request):
            body = await request.json()
            if inline_in_user_message(body):
                self.refused.append(body)
                return JSONResponse({"detail": "Invalid request body."}, status_code=422)
            if self.limited_until is None:
                self.limited_until = time.monotonic() + rate_limited_for
            if len(self.limited) < rate_limited or time.monotonic() < self.limited_until:
                self.limited.append(body)
                limited = [
                    sse("response.created", {"type": "response.created",
                        "response": {"id": f"resp_limited_{len(self.limited)}", "status": "in_progress"}}),
                    sse("response.failed", {"type": "response.failed", "response": {
                        "id": f"resp_limited_{len(self.limited)}", "status": "failed", "error": {
                            "code": "rate_limit_exceeded", "message": RATE_LIMITED}}}),
                ]

                async def failing():
                    for event in limited:
                        yield event

                return StreamingResponse(failing(), media_type="text/event-stream")
            if subagent and any(isinstance(item, dict) and item.get("type") == "agent_message"
                                and str(item.get("recipient", "")).endswith("/helper")
                                for item in body.get("input", [])):
                self.helper_requests.append(body)
                helper_asked.set()
                return StreamingResponse(iter(message_events("helper", "helper done", body.get("model"))),
                                         media_type="text/event-stream")
            n = next(counter)
            self.requests.append(body)
            raw = json.dumps(body)
            if subagent and n > 1:
                # A turn over before the helper asks anything ends it with Codex.
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(helper_asked.wait(), 60)
            loaded = re.search(rf"mcp__{MCP_SERVER}\.{MCP_TOOL}", raw) if tool_search and n == 2 else None
            if n == 1 or loaded:
                name, arguments = code_mode_call(raw) if lite else shell_call(raw)
                self.shell_tool = name
                items = [transport_call(1, name, arguments)]
                if tool_search:
                    items = [transport_call(n, "tool_search", {"query": "echo probe"}) if n == 1
                             else transport_call(n, loaded.group(0), {"text": "bridge-e2e-42"})]
                if imagegen:
                    items = [transport_call(1, "image_gen.imagegen", {"prompt": IMAGE_PROMPT})]
                if parallel:
                    items.append(transport_call(2, *shell_call(raw, SECOND_PROBE)))
                if subagent:
                    items.append(transport_call(len(items) + 1, "collaboration.spawn_agent",
                                                {"message": SUBAGENT_TASK, "task_name": "helper"}))
                events = [sse("response.created", {"type": "response.created",
                              "response": {"id": f"resp_{n}", "status": "in_progress", "output": []}})]
                for index, item in enumerate(items):
                    events += [
                        sse("response.output_item.added", {"type": "response.output_item.added",
                            "output_index": index, "item": {**item, "arguments": "", "status": "in_progress"}}),
                        sse("response.function_call_arguments.delta", {
                            "type": "response.function_call_arguments.delta", "output_index": index,
                            "item_id": item["id"], "delta": item["arguments"]}),
                        sse("response.function_call_arguments.done", {
                            "type": "response.function_call_arguments.done", "output_index": index,
                            "item_id": item["id"], "arguments": item["arguments"]}),
                        sse("response.output_item.done", {"type": "response.output_item.done",
                            "output_index": index, "item": item}),
                    ]
                events.append(sse("response.completed", {"type": "response.completed", "response": {
                    "id": f"resp_{n}", "status": "completed", "model": body.get("model"),
                    "output": items, "usage": USAGE}}))
            else:
                seen = "bridge-e2e-42" in raw and (not parallel or "bridge-e2e-43" in raw)
                if imagegen:
                    seen = f"data:image/png;base64,{self.drawn}" in raw
                if tool_search:
                    seen = MCP_ECHO in raw
                text = "done: tool output seen" if seen else "done: tool output MISSING"
                events = message_events(n, text, body.get("model"))

            async def stream():
                for event in events:
                    yield event

            return StreamingResponse(stream(), media_type="text/event-stream")

        @app.api_route("/{path:path}", methods=["GET", "POST"])
        async def other(path: str):
            self.unexpected.append(path)
            return JSONResponse({"error": {"message": "not here"}}, status_code=404)

        self.app = app


def message_events(n: object, text: str, model: object) -> list[bytes]:
    msg = {"type": "message", "id": f"msg_{n}", "role": "assistant", "status": "completed",
           "content": [{"type": "output_text", "text": text, "annotations": []}]}
    return [
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
            "id": f"resp_{n}", "status": "completed", "model": model,
            "output": [msg], "usage": USAGE}}),
    ]


RATE_LIMITED = ("Rate limit reached for gpt-5.6-sol in organization org-e2e on tokens per min (TPM): "
                "Limit 500000000, Used 499990000, Requested 150000. Please try again in 18ms.")
# --rate-limited long: limited for longer than Codex, told to, waits for a silent stream.
LONG_RATE_LIMIT_SECONDS = 40
CODEX_IDLE_MS = 20000


# --network-drop: every connection to the backend is cut for this long after the first one.
NETWORK_DROP_SECONDS = 12


class Gate:
    """Passes connections on to the fake backend, but cuts requests to it for ``drop`` seconds after the first.

    Only POSTs: the bridge's look-up of the exit IP on the same host goes through.
    """

    def __init__(self, port: int, drop: float) -> None:
        self.target, self.drop = port, drop
        self.cut = 0
        self.down_until: float | None = None
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _down_for(self, data: bytes) -> bool:
        if not data.startswith(b"POST"):
            return False
        now = time.monotonic()
        if self.down_until is None:
            self.down_until = now + self.drop
        return now < self.down_until

    def _cut(self, client: socket.socket) -> None:
        self.cut += 1
        # Reset rather than closed, the way a connection through a node that went away ends.
        linger = struct.pack("HH" if sys.platform == "win32" else "ii", 1, 0)
        with contextlib.suppress(OSError):
            client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
        client.close()

    def _handle(self, client: socket.socket) -> None:
        try:
            upstream = socket.create_connection(("127.0.0.1", self.target))
        except OSError:
            client.close()
            return

        def back() -> None:
            with contextlib.suppress(OSError):
                while data := upstream.recv(65536):
                    client.sendall(data)
            with contextlib.suppress(OSError):
                client.shutdown(socket.SHUT_WR)

        replies = threading.Thread(target=back, daemon=True)
        replies.start()
        with contextlib.suppress(OSError):
            # On a kept-alive connection each request starts a read of its own.
            while data := client.recv(65536):
                if self._down_for(data):
                    self._cut(client)
                    break
                upstream.sendall(data)
        with contextlib.suppress(OSError):
            upstream.shutdown(socket.SHUT_WR)
        replies.join(timeout=30)
        client.close()
        upstream.close()

    def close(self) -> None:
        self.sock.close()


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


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def healthy(port: int) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
            return response.status == 200
    except OSError:
        return False


def run(command: list[str], *, cwd: Path, env: dict, timeout: int) -> subprocess.CompletedProcess:
    print("$", subprocess.list2cmdline(command), flush=True)
    return subprocess.run(
        command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        timeout=timeout, text=True, encoding="utf-8", errors="replace",
    )


def run_launcher(launcher, args, webview: Path, project: Path, env: dict) -> tuple[str, list]:
    command = [*launcher, "--webview-dir", str(webview), "--model", args.model, "--", *args.codex_args]
    result = run(command, cwd=project, env=env, timeout=args.timeout)
    return result.stdout, [(result.returncode == 0, f"launcher exit code {result.returncode}")]


def in_background(command: list[str], *, cwd: Path, env: dict, log: Path) -> subprocess.Popen:
    """``command`` in a process group of its own, so that ``stop`` reaches it like Ctrl+C would."""
    print("$", subprocess.list2cmdline(command), "&", flush=True)
    group = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
        else {"start_new_session": True}
    )
    with open(log, "wb") as out:
        return subprocess.Popen(
            command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, **group
        )


def wait_healthy(process: subprocess.Popen, port: int, timeout: int) -> bool:
    deadline = time.monotonic() + timeout
    while not healthy(port) and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.5)
    return healthy(port)


def stop(process: subprocess.Popen) -> int:
    if process.poll() is None:
        if sys.platform == "win32":
            os.kill(process.pid, signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(process.pid, signal.SIGINT)
    try:
        return process.wait(timeout=60)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.wait()


def loopback_direct(env: dict) -> dict:
    """``env`` with the bridge on 127.0.0.1 kept away from any proxy."""
    env = dict(env)
    for key in ("NO_PROXY", "no_proxy"):
        env[key] = ",".join(filter(None, [env.get(key, ""), "127.0.0.1", "localhost"]))
    return env


def run_desktop(launcher, args, root: Path, webview: Path, project: Path, env: dict) -> tuple[str, list]:
    config = Path(env["CODEX_HOME"]) / "config.toml"
    config.write_bytes(USER_CONFIG.encode())
    port = free_port()
    # On Windows, desktop also sets the system timezone to the exit's; put back afterwards.
    windows_zone = tzutil("/g") if sys.platform == "win32" else None
    log = root / "desktop.log"
    desktop = in_background(
        [*launcher, "desktop", "--webview-dir", str(webview), "--model", args.model, "--port", str(port)],
        cwd=project, env=env, log=log,
    )
    checks = []
    output = ""
    try:
        checks.append((wait_healthy(desktop, port, args.timeout), "the desktop bridge did not come up"))
        enabled = config.read_text(encoding="utf-8")
        provider = "openai" if args.shared else "excel-bridge"
        checks.append((f"model_provider = '{provider}'" in enabled
                       and (f"openai_base_url = 'http://127.0.0.1:{port}/v1'" in enabled) == args.shared,
                       "config.toml was not pointed at the bridge"))
        codex = shutil.which("codex", path=env.get("PATH"))
        if codex and healthy(port):
            result = run([codex, *args.codex_args], cwd=project, env=loopback_direct(env), timeout=args.timeout)
            output = result.stdout
            checks.append((result.returncode == 0, f"codex exit code {result.returncode}"))
        else:
            checks.append((False, "codex not found on PATH" if not codex else "skipped codex"))
    finally:
        code = stop(desktop)
    desktop_output = log.read_text(encoding="utf-8", errors="replace")
    print("--- desktop window\n" + desktop_output)
    backup = config.with_name("config.toml.before-excel-codex")
    checks += [
        (code == 0, f"desktop exit code {code}"),
        (config.read_bytes() == USER_CONFIG.encode(), "config.toml was not restored exactly"),
        (backup.exists() and backup.read_bytes() == USER_CONFIG.encode(), "no exact backup of config.toml"),
    ]
    if windows_zone is not None:
        changed = "Windows timezone changed from" in desktop_output
        # Leaving the window puts it back by itself; `timezone restore` has nothing left to do.
        after_exit = tzutil("/g")
        restored = run([*launcher, "timezone", "restore"], cwd=project, env=env, timeout=120)
        now = tzutil("/g")
        if now != windows_zone:
            tzutil("/s", windows_zone)
        checks += [
            (changed, "desktop did not set the Windows timezone to the exit's"),
            (after_exit == windows_zone and f"Windows timezone: put back {windows_zone}." in desktop_output,
             f"leaving desktop left the Windows timezone at {after_exit} instead of {windows_zone}"),
            (restored.returncode == 0 and "Nothing to restore" in restored.stdout and now == windows_zone,
             f"`timezone restore` after desktop: {restored.stdout.strip()!r}, now {now}"),
        ]
    return output, checks


def sign_codex_in(home: Path) -> None:
    """A made-up API key sign-in for Codex itself: the bridge then stands in for its openai provider."""
    (home / "auth.json").write_text(
        json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": "sk-e2e-not-a-key"}), encoding="utf-8")


def migrate_then_resume(launcher, args, root: Path, webview: Path, project: Path, env: dict) -> tuple[str, list]:
    """A conversation filed the 0.5.3 way, moved into the shared list, then carried on without the bridge's provider."""
    home = Path(env["CODEX_HOME"])
    # This machine may run a Codex of its own; this run's Codex home is used by nothing else.
    env = dict(env, EXCEL_BRIDGE_ASSUME_CODEX_QUIT="1")
    if args.migrate == "relay":
        # The relay's key, as its template has it in auth.json.
        sign_codex_in(home)
        output, checks = through_a_relay(launcher, args, root, webview, project, env)
        filed_under = RELAY_PROVIDER
    else:
        first = shlex.split(args.first_launcher) if args.first_launcher else launcher
        output, checks = run_launcher(first, args, webview, project, env)
        filed_under = codex_config.PROVIDER_ID
    args.first_requests = len(args.backend.requests)
    before = codex_threads(home)
    filed = rollout_providers(home)
    checks += [
        (len(before) == 1 and before[0][0] == filed_under,
         f"the first conversation should be filed under {filed_under}, got {before}"),
        (filed == [filed_under], f"the first conversation's file should name {filed_under}, got {filed}"),
    ]
    sign_codex_in(home)
    if args.migrate == "desktop":
        moved, moved_checks = desktop_moves_them(launcher, args, root, webview, project, env)
        expected = "Moved 1 conversation(s) from the bridge's own provider into the shared list"
    elif args.migrate == "relay":
        # The plain list points at them; `--from` moves them.
        listed = run([*launcher, "threads"], cwd=project, env=env, timeout=120)
        result = run([*launcher, "threads", "migrate", "--from", RELAY_PROVIDER], cwd=project, env=env, timeout=120)
        moved = listed.stdout + result.stdout
        moved_checks = [
            (listed.returncode == 0 and f"Also filed under other providers: `{RELAY_PROVIDER}` (1)" in listed.stdout,
             f"`threads` did not point at the conversation: {listed.stdout.strip()[-1500:]}"),
            (result.returncode == 0, f"`threads migrate --from` exit code {result.returncode}"),
        ]
        expected = f"Moved 1 conversation(s) from `{RELAY_PROVIDER}` to `openai`"
    else:
        result = run([*launcher, "threads", "migrate"], cwd=project, env=env, timeout=120)
        moved, moved_checks = result.stdout, [(result.returncode == 0, f"`threads migrate` exit code {result.returncode}")]
        expected = "Moved 1 conversation(s) into the list shared"
    print(moved)
    after = codex_threads(home)
    refiled = rollout_providers(home)
    rebuilt = rollout_rebuilt(home)
    # A 1M model too: OpenAI serves the same model without it.
    official = codex_config.official_model(args.model)
    checks += moved_checks + [
        (expected in moved, f"the conversation was not moved: {moved.strip()[-1500:]}"),
        (after == [("openai", official)], f"it should be filed as ('openai', {official}), got {after}"),
        (refiled == ["openai"], f"its file should name openai now, got {refiled}"),
        (rebuilt == after, f"Codex would rebuild its index row from the file as {rebuilt}, not {after}"),
        (len(list(home.glob("state_*.sqlite.before-excel-codex-*"))) == 1, "no copy of Codex's index"),
    ]
    args.shared = True
    args.codex_args = list(RESUME_ARGS)
    output, resumed = resume_with_codex_providers_only(launcher, args, root, webview, project, env)
    carried = args.backend.requests[args.first_requests:]
    checks += resumed + [
        (len(carried) == 1 and "bridge-e2e-42" in json.dumps(carried[0]),
         "`resume --last` did not carry the moved conversation on"),
    ]
    return output, checks


def through_a_relay(launcher, args, root: Path, webview: Path, project: Path, env: dict,
                    catalog: bool = True) -> tuple[str, list]:
    """A conversation started through a relay's own provider named ``OpenAI``; ``serve`` stands in for the relay.

    ``catalog=False`` leaves the bridge's model entries out, as a relay's
    template does: Codex then sends this model's tools the Responses Lite way.
    """
    with serving(launcher, args, root, webview, project, env, "relay.log") as (port, up):
        codex = shutil.which("codex", path=env.get("PATH"))
        if not (up and codex):
            return "", [(up, "`serve` did not come up"), (bool(codex), "codex not found on PATH")]
        table = f"model_providers.{RELAY_PROVIDER}"
        overrides = [
            "-c", f"model_provider='{RELAY_PROVIDER}'",
            "-c", f"{table}.name='{RELAY_PROVIDER}'",
            "-c", f"{table}.base_url='{codex_config.base_url(port)}'",
            "-c", f"{table}.wire_api='responses'",
            # The key comes from auth.json's OPENAI_API_KEY, as with the relay's template.
            "-c", f"{table}.requires_openai_auth=true",
            "-c", f"model='{codex_config.codex_model(args.model)}'",
        ]
        if catalog:
            overrides += ["-c", f"model_catalog_json='{codex_config.write_catalog(root / 'catalog')}'"]
        result = run(codex_config.codex_command(codex, overrides, args.codex_args),
                     cwd=project, env=loopback_direct(env), timeout=args.timeout)
    return result.stdout, [
        (result.returncode == 0, f"codex through the relay: exit code {result.returncode}: "
                                 f"{result.stdout.strip()[-1500:]}"),
    ]


@contextlib.contextmanager
def serving(launcher, args, root: Path, webview: Path, project: Path, env: dict, log: str):
    """``serve`` on a port of its own while in the block: (port, whether it came up)."""
    port = free_port()
    bridge = in_background([*launcher, "serve", "--webview-dir", str(webview), "--port", str(port)],
                           cwd=project, env=env, log=root / log)
    try:
        yield port, wait_healthy(bridge, port, args.timeout)
    finally:
        stop(bridge)


def desktop_moves_them(launcher, args, root: Path, webview: Path, project: Path, env: dict) -> tuple[str, list]:
    """Start `desktop` with Codex quit, then leave it: what it printed, and that it put everything back."""
    config = Path(env["CODEX_HOME"]) / "config.toml"
    config.write_bytes(USER_CONFIG.encode())
    windows_zone = tzutil("/g") if sys.platform == "win32" else None
    port = free_port()
    desktop = in_background(
        [*launcher, "desktop", "--webview-dir", str(webview), "--model", args.model, "--port", str(port)],
        cwd=project, env=env, log=root / "desktop.log",
    )
    try:
        up = wait_healthy(desktop, port, args.timeout)
    finally:
        code = stop(desktop)
        if windows_zone is not None and tzutil("/g") != windows_zone:
            tzutil("/s", windows_zone)
    return _desktop_log(root), [
        (up, "the desktop bridge did not come up"),
        (code == 0, f"desktop exit code {code}"),
        (config.read_bytes() == USER_CONFIG.encode(), "config.toml was not restored exactly"),
    ]


def resume_with_codex_providers_only(launcher, args, root: Path, webview: Path, project: Path,
                                     env: dict) -> tuple[str, list]:
    """``codex exec resume --last`` with no ``excel-bridge`` provider, as with the bridge off.

    Codex's ``openai`` provider reaches the bridge through ``serve``, standing
    in for the official service.  A conversation still filed under
    ``excel-bridge`` fails here: "Model provider `excel-bridge` not found".
    """
    checks = []
    output = ""
    with serving(launcher, args, root, webview, project, env, "serve.log") as (port, up):
        checks.append((up, "`serve` did not come up"))
        codex = shutil.which("codex", path=env.get("PATH"))
        if codex and healthy(port):
            base = ["-c", f"openai_base_url='{codex_config.base_url(port)}'"]
            # The desktop app opens it through `codex app-server`, which takes the provider from its file.
            opened, said = app_server_opens(codex, base, codex_thread_ids(Path(env["CODEX_HOME"])),
                                            project, loopback_direct(env), args.timeout)
            # `codex exec resume` takes Codex's default model; the desktop app keeps the conversation's.
            overrides = [*base, "-c", f"model='{codex_config.codex_model(args.model)}'"]
            result = run(codex_config.codex_command(codex, overrides, args.codex_args),
                         cwd=project, env=loopback_direct(env), timeout=args.timeout)
            output = result.stdout
            checks += [
                (opened, f"the desktop app's `thread/resume` could not open it: {said}"),
                (result.returncode == 0, f"codex exit code {result.returncode}: {output.strip()[-1500:]}"),
                ("`excel-bridge` not found" not in output, "Codex still looked for the excel-bridge provider"),
            ]
        else:
            checks.append((False, "codex not found on PATH" if not codex else "skipped codex"))
    return output, checks


def app_server_opens(codex: str, overrides: list[str], thread_ids: list[str], project: Path, env: dict,
                     timeout: int) -> tuple[bool, str]:
    """Open the one conversation the way the desktop app does: `codex app-server`, list, `thread/resume`.

    Listing matters: Codex rebuilds each listed row from its conversation file.
    """
    if len(thread_ids) != 1:
        return False, f"expected one conversation, got {thread_ids}"
    command = [codex, *overrides, "app-server"]
    print("$", subprocess.list2cmdline(command), flush=True)
    server = subprocess.Popen(command, cwd=project, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", bufsize=1)
    lines: queue.Queue = queue.Queue()
    threading.Thread(target=lambda: [lines.put(line) for line in server.stdout], daemon=True).start()

    def send(message: dict) -> None:
        server.stdin.write(json.dumps(message) + "\n")
        server.stdin.flush()

    def call(request_id: int, method: str, params: dict) -> dict:
        send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                message = json.loads(lines.get(timeout=1))
            except (queue.Empty, ValueError):
                continue
            if message.get("id") == request_id and "method" not in message:
                return message
        raise TimeoutError(method)

    try:
        call(1, "initialize", {"clientInfo": {"name": "excel-codex-e2e", "title": None, "version": "0"},
                               "capabilities": None})
        send({"method": "initialized"})
        # The desktop app lists its own (interactive) ones; this one was started by `codex exec`.
        listed = call(2, "thread/list", {"modelProviders": [], "sourceKinds": ["cli", "vscode", "exec"]})
        reply = call(3, "thread/resume", {"threadId": thread_ids[0], "excludeTurns": True})
    except (OSError, TimeoutError) as exc:
        return False, repr(exc)
    finally:
        # Gone for good before `codex exec resume`: a live app-server stays the conversation's writer.
        # On Windows `codex` is a .cmd wrapper, and terminating it leaves Codex itself running.
        with contextlib.suppress(OSError):
            server.stdin.close()
        try:
            server.wait(timeout=30)
        except subprocess.TimeoutExpired:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(server.pid)], capture_output=True)
            else:
                server.kill()
            server.wait(timeout=30)
    shown = [(item.get("id"), item.get("modelProvider")) for item in (listed.get("result") or {}).get("data") or []]
    if shown != [(thread_ids[0], "openai")]:
        return False, f"listed as {shown}"
    if "error" in reply:
        return False, json.dumps(reply["error"])
    provider = (reply.get("result") or {}).get("modelProvider")
    return provider == "openai", f"provider {provider}"


def codex_thread_ids(home: Path) -> list[str]:
    found = []
    for path in sorted(home.glob("state_*.sqlite")):
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            found += [row[0] for row in connection.execute("SELECT id FROM threads")]
        finally:
            connection.close()
    return found


def rollout_providers(home: Path) -> list[str]:
    """The provider on the first line of each conversation file in this run's own Codex home."""
    found = []
    for path in sorted((home / "sessions").rglob("rollout-*.jsonl")):
        with path.open(encoding="utf-8") as handle:
            found.append(json.loads(handle.readline())["payload"].get("model_provider"))
    return found


def rollout_rebuilt(home: Path) -> list[tuple[str, str]]:
    """(provider, model) Codex takes from each conversation file when it rebuilds its index row.

    As codex-rs state/src/extract.rs does: the provider from session_meta, then
    the model, and the provider again, from each later line in turn.
    """
    found = []
    for path in sorted((home / "sessions").rglob("rollout-*.jsonl")):
        provider = model = None
        for line in path.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            payload = item.get("payload") or {}
            if item.get("type") == "session_meta":
                provider = payload.get("model_provider")
            elif item.get("type") == "turn_context":
                model = payload.get("model")
            elif item.get("type") == "event_msg" and payload.get("type") == "thread_settings_applied":
                model = payload["thread_settings"].get("model")
                provider = payload["thread_settings"].get("model_provider_id")
        found.append((provider, model))
    return found


def codex_threads(home: Path) -> list[tuple[str, str]]:
    """(provider, model) of each conversation in this run's own Codex home."""
    found = []
    for path in sorted(home.glob("state_*.sqlite")):
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            found += connection.execute("SELECT model_provider, model FROM threads").fetchall()
        finally:
            connection.close()
    return found


def tzutil(*args: str) -> str:
    return subprocess.run(["tzutil", *args], capture_output=True, text=True, check=True).stdout.strip()


def _annotation(text: str) -> str:
    """``text`` as one GitHub Actions workflow-command message."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _desktop_log(root: Path) -> str:
    log = root / "desktop.log"
    return log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default="gpt-5.6-sol-excel")
    parser.add_argument("--desktop", action="store_true", help="check `desktop` mode with a plain codex")
    parser.add_argument("--images", action="store_true", help="also attach a picture")
    parser.add_argument("--parallel", action="store_true", help="answer with two tool calls at once")
    parser.add_argument("--imagegen", action="store_true", help="answer with a call to Codex's image tool")
    parser.add_argument("--codex-login", nargs="?", const="accepted", choices=["accepted", "refused"],
                        help="also sign Codex in with ChatGPT; `refused` makes the backend turn it down")
    parser.add_argument("--shared", action="store_true",
                        help="sign Codex itself in, so the bridge stands in for its openai provider")
    parser.add_argument("--migrate", nargs="?", const="command", choices=["command", "desktop", "relay"],
                        help="file a conversation the 0.5.3 way, move it into the shared list (by "
                        "`threads migrate`, or by starting `desktop`), then carry it on with the bridge's "
                        "provider gone; `relay`: file it under a relay's own `OpenAI` provider and move it "
                        "with `threads migrate --from OpenAI`")
    parser.add_argument("--rate-limited", nargs="?", const="briefly", choices=("briefly", "long"),
                        help="fail the first two requests on the shared tokens-per-minute limit, or (long) "
                        f"all in the first {LONG_RATE_LIMIT_SECONDS} s with Codex dropping a stream silent for "
                        f"{CODEX_IDLE_MS // 1000} s; the bridge waits them out")
    parser.add_argument("--subagent", action="store_true",
                        help="also spawn a helper agent (Codex's multi_agent_v2); it must get the task as text")
    parser.add_argument("--ultra", action="store_true",
                        help="like --subagent, with Codex's ultra effort instead of the feature switch; "
                        "the backend is asked for xhigh")
    parser.add_argument("--tool-search", action="store_true",
                        help="give Codex an MCP server: the model must find its tool with tool_search, then call it")
    parser.add_argument("--lite", action="store_true",
                        help="run Codex through a relay without the bridge's model entries, so it sends "
                        "the tools the Responses Lite way and calls them through code mode's exec")
    parser.add_argument("--network-drop", action="store_true",
                        help=f"cut every connection to the backend for {NETWORK_DROP_SECONDS} s after the first; "
                        "the bridge keeps trying")
    parser.add_argument("--first-launcher",
                        help="with --migrate: the command that starts that conversation, e.g. a 0.5.3 checkout's")
    parser.add_argument("--timeout", type=int, default=900, help="seconds for each long step")
    parser.add_argument("launcher", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    # What a launcher printed may not fit the console's code page (cp1252 on Windows runners).
    sys.stdout.reconfigure(errors="backslashreplace")
    launcher = args.launcher[1:] if args.launcher[:1] == ["--"] else args.launcher
    if not launcher:
        parser.error("give the launcher command after --")
    if args.rate_limited == "long" and (args.shared or args.migrate or args.lite):
        parser.error("--rate-limited long sets the idle timeout of the bridge's own provider")
    if args.lite and (args.shared or args.migrate or args.desktop):
        parser.error("--lite runs Codex through a relay of its own")
    if args.ultra and (args.lite or args.subagent):
        parser.error("--ultra needs the bridge's model entries, and no feature switch")
    if args.tool_search and (args.lite or args.parallel or args.imagegen or args.subagent or args.ultra):
        parser.error("--tool-search needs the bridge's model entries and a first answer of its own")
    if Path(launcher[0]).exists():
        launcher[0] = str(Path(launcher[0]).resolve())

    root = Path(tempfile.mkdtemp(prefix="excel-codex-e2e-"))
    webview = write_webview_session(root / "webview", time.time() + 3 * 86400, account="e2e-account")
    project = root / "project"
    project.mkdir()
    # Never whoever runs this: a made-up Codex sign-in, or none at all.
    codex_auth = root / "codex-login" / "auth.json"
    if args.codex_login:
        write_codex_login(codex_auth, time.time() + 3 * 86400, account="e2e-codex-account")
    started_at = dt.datetime.now(dt.timezone.utc)
    exit_zone = exit_zone_for(started_at)
    backend = FakeExcelBackend(parallel=args.parallel, imagegen=args.imagegen,
                               refuse="e2e-codex-account" if args.codex_login == "refused" else None,
                               exit_zone=exit_zone, rate_limited=2 if args.rate_limited == "briefly" else 0,
                               rate_limited_for=LONG_RATE_LIMIT_SECONDS if args.rate_limited == "long" else 0,
                               subagent=args.subagent or args.ultra, lite=args.lite,
                               tool_search=args.tool_search)
    server, port = start_server(backend.app)
    gate = Gate(port, NETWORK_DROP_SECONDS) if args.network_drop else None

    env = dict(os.environ)
    env.update(
        CODEX_HOME=str(root / "codex-home"),
        EXCEL_BRIDGE_CODEX_AUTH=str(codex_auth),
        EXCEL_BRIDGE_HOME=str(root / "bridge-home"),
        GHCP_EXCEL_RESPONSES_URL=f"http://127.0.0.1:{gate.port if gate else port}/basispoints/api/responses",
        EXCEL_BRIDGE_TIMEZONE="auto",
        EXCEL_BRIDGE_TIMEZONE_LOOKUP=f"http://127.0.0.1:{port}/geo/{{ip}}",
        EXCEL_BRIDGE_TIMEZONE_TRACE=f"http://127.0.0.1:{port}/cdn-cgi/trace",
        PYTHONPATH=USER_PYTHONPATH,
    )
    Path(env["CODEX_HOME"]).mkdir()
    if args.shared and not args.migrate:
        sign_codex_in(Path(env["CODEX_HOME"]))
    args.codex_args = list(CODEX_ARGS)
    if args.rate_limited == "long":
        args.codex_args[-1:-1] = ["-c", f"model_providers.excel-bridge.stream_idle_timeout_ms={CODEX_IDLE_MS}"]
    if args.subagent:
        args.codex_args[-1:-1] = ["-c", "features.multi_agent_v2=true"]
    if args.ultra:
        args.codex_args[args.codex_args.index("model_reasoning_effort=high")] = "model_reasoning_effort=ultra"
    if args.tool_search:
        fake_mcp = root / "fake_mcp.py"
        fake_mcp.write_text(FAKE_MCP, encoding="utf-8")
        # TOML literal strings: a Windows path's backslashes stay as they are.
        args.codex_args[-1:-1] = ["-c", f"mcp_servers.{MCP_SERVER}.command='{sys.executable}'",
                                  "-c", f"mcp_servers.{MCP_SERVER}.args=['{fake_mcp}']"]
    args.backend, args.first_requests = backend, 0
    picture = png()
    if args.images:
        (project / "picture.png").write_bytes(picture)
        # -i takes every argument after it, so it goes last.
        args.codex_args += ["-i", str(project / "picture.png")]
    started = time.monotonic()
    if args.migrate:
        output, checks = migrate_then_resume(launcher, args, root, webview, project, env)
    elif args.lite:
        # The relay's key, as its template has it in auth.json.
        sign_codex_in(Path(env["CODEX_HOME"]))
        output, checks = through_a_relay(launcher, args, root, webview, project, env, catalog=False)
    elif args.desktop:
        output, checks = run_desktop(launcher, args, root, webview, project, env)
    else:
        output, checks = run_launcher(launcher, args, webview, project, env)
    server.should_exit = True
    if gate:
        gate.close()

    upstream_model = excel_upstream.EXCEL_MODEL_UPSTREAMS[args.model]
    # A --first-launcher from before 0.5.4 does not move the timezone.
    sent = json.dumps(backend.requests[args.first_requests if args.first_launcher else 0:])
    zones = set(re.findall(r"<timezone>([^<]*)</timezone>", sent))
    dates = set(re.findall(r"<current_date>([^<]*)</current_date>", sent))
    exit_days = {exit_timezone.today_in(exit_zone, now=when)
                 for when in (started_at, dt.datetime.now(dt.timezone.utc))}
    # Each bridge started looks the exit up once (--migrate starts a second to carry on);
    # on Windows desktop's system sync does too.
    lookups = 1 + bool(args.migrate) + (sys.platform == "win32" and (args.desktop or args.migrate == "desktop"))
    checks += [
        (0 < len(backend.lookups) <= lookups and set(backend.lookups) == {EXIT_IP},
         f"expected the exit IP looked up at most {lookups} time(s), got {backend.lookups}"),
        (zones == {exit_zone}, f"Codex's timezone should be the exit's ({exit_zone}), got {zones}"),
        (bool(dates) and dates <= exit_days, f"Codex's date should be the day at the exit {exit_days}, got {dates}"),
        (f"provider: {RELAY_PROVIDER if args.lite else 'openai' if args.shared else 'excel-bridge'}" in output,
         "Codex used another provider"),
        ("done: tool output seen" in output, "the tool output did not reach the model"),
        (len(backend.requests) >= 2, f"expected 2+ upstream requests, got {len(backend.requests)}"),
        (all(r.get("model") == upstream_model for r in backend.requests),
         f"upstream model is not {upstream_model}"),
        (all(r.get("reasoning_effort") == ("xhigh" if args.ultra else "high") for r in backend.requests),
         "reasoning effort from the subcommand -c did not arrive"),
        (args.imagegen or args.tool_search or len(backend.requests) >= 2
         and f"PP=[{USER_PYTHONPATH}]" in json.dumps(backend.requests[1]),
         "Codex's commands saw a different PYTHONPATH"),
        (not backend.unexpected, f"unexpected upstream paths: {backend.unexpected}"),
    ]
    # Every request carries one sign-in: Codex's when it is there and taken, else the Excel one.
    signed_in = "e2e-codex-account" if args.codex_login == "accepted" else "e2e-account"
    served = backend.accounts
    if args.codex_login == "refused":
        checks.append((served[:1] == ["e2e-codex-account"] and served.count("e2e-codex-account") == 1,
                       f"expected one try with the refused Codex sign-in first, got {served}"))
        served = served[1:]
    checks.append((bool(served) and set(served) == {signed_in},
                   f"expected every request on {signed_in}, got {backend.accounts}"))
    if args.shared:
        threads = codex_threads(Path(env["CODEX_HOME"]))
        official = codex_config.codex_model(args.model)
        # A helper agent is a conversation of its own.
        checks.append((threads == [("openai", official)] * (2 if args.subagent or args.ultra else 1),
                       f"the conversation should be filed as the official sign-in files it, got {threads}"))
    if args.parallel and len(backend.requests) >= 2:
        replayed = [(item.get("type"), item.get("call_id") if item.get("type") == "function_call_output"
                     else item.get("id"))
                    for item in backend.requests[1].get("input", [])
                    if isinstance(item, dict)
                    and (item.get("name") == "run_officejs" or item.get("type") == "function_call_output")]
        checks += [
            (len(backend.requests) == 2, f"expected exactly 2 upstream requests, got {len(backend.requests)}"),
            (replayed == [("function_call", "fc_1"), ("function_call", "fc_2"),
                          ("function_call_output", "call_1"), ("function_call_output", "call_2")],
             f"both native calls then both results should be replayed, got {replayed}"),
            (backend.requests[1].get("metadata", {}).get("agent_iteration") == "2",
             "parallel results should count as one agent iteration"),
        ]
    if args.imagegen:
        headers, drawing = backend.drawings[0] if backend.drawings else ({}, {})
        checks += [
            (bool(backend.requests) and "image_gen" in json.dumps(backend.requests[0]),
             "Codex did not offer its image tool"),
            (len(backend.drawings) == 1, f"expected one picture drawn, got {len(backend.drawings)}"),
            # Codex 0.156 leaves the background out; later builds send "opaque".
            ({**drawing, "background": "auto"} == {"background": "auto", "model": "gpt-image-2",
                                                   "output_format": "png", "prompt": IMAGE_PROMPT,
                                                   "quality": "auto", "size": "auto"}
             and drawing.get("background") in {"auto", "opaque"},
             f"the image request was not the add-in's: {drawing}"),
            (headers.get("authorization", "").startswith("Bearer ")
             and headers.get("chatgpt-account-id") == signed_in,
             "the image request did not carry the sign-in"),
        ]
    if args.images:
        announced = "Pictures: sent to OpenAI"
        sent = [pictures(body.get("input")) for body in backend.requests]
        file_ids = sorted({part.get("file_id") for parts in sent for part in parts})
        upload_headers, upload = backend.uploads[0] if backend.uploads else ({}, b"")
        checks += [
            (announced in output + _desktop_log(root), f"the bridge did not announce {announced!r}"),
            (len(backend.refused) == 1, f"expected one inline try, got {len(backend.refused)} refused"),
            (len(backend.uploads) == 1, f"expected the picture uploaded once, got {len(backend.uploads)}"),
            (upload_headers.get("content-type", "").startswith("multipart/form-data")
             and upload_headers.get("authorization", "").startswith("Bearer ")
             and upload_headers.get("chatgpt-account-id") == signed_in and picture in upload,
             "the upload was not the add-in's"),
            (bool(sent) and all(len(parts) == 1 for parts in sent),
             f"expected the picture once in every request, got {[len(parts) for parts in sent]}"),
            (file_ids == ["file-e2e-1"], f"expected every request to name the upload, got {file_ids}"),
        ]
    if args.rate_limited:
        bridge_log = root / "bridge-home" / "bridge.log"
        waited = bridge_log.read_text(encoding="utf-8", errors="replace") if bridge_log.exists() else ""
        waited += _desktop_log(root)
        expected = 2 if args.rate_limited == "briefly" else 5
        checks += [
            (len(backend.limited) >= expected if args.rate_limited == "long" else len(backend.limited) == expected,
             f"expected {expected} rate-limited requests, got {len(backend.limited)}"),
            (bool(backend.limited) and bool(backend.requests) and backend.limited[0] == backend.requests[0],
             "the bridge did not send the rate-limited request again as it was"),
            (waited.count("the Excel backend is rate limited") == len(backend.limited),
             "the bridge did not say it was waiting"),
            (not any(said in output.lower() for said in ("rate limit", "reconnecting", "disconnected")),
             "Codex saw the rate limit, or gave up on the stream"),
        ]
    # Codex's developer message for ultra; below it, one saying to spawn only when asked.
    delegating = "proactive multi-agent delegation is active" in (
        json.dumps(backend.requests[0]).lower() if backend.requests else "")
    if args.ultra:
        # Codex sends ultra as the entries' multi_agent_reasoning_effort, and only
        # multi-agent v2 (turned on by the entries alone here) tells the model to delegate.
        efforts = [body.get("reasoning_effort") for body in backend.requests + backend.helper_requests]
        checks += [
            (bool(efforts) and set(efforts) == {"xhigh"}, f"expected xhigh in every request, got {efforts}"),
            (delegating, "Codex did not tell the model to hand work to helpers unasked"),
        ]
    if args.subagent:
        checks.append((not delegating, "Codex told the model to hand work to helpers unasked"))
    if args.subagent or args.ultra:
        # Codex labels the task encrypted unless the call says its arguments are
        # plain; the backend then cannot read it ("encrypted content ... could not be decoded").
        told = [part for body in backend.helper_requests for item in body.get("input", [])
                if isinstance(item, dict) and item.get("type") == "agent_message"
                for part in item.get("content", []) if isinstance(part, dict)]
        checks += [
            (bool(backend.requests) and "collaboration.spawn_agent" in json.dumps(backend.requests[0]),
             "Codex did not offer spawn_agent"),
            (bool(backend.helper_requests), "the helper never asked the backend anything"),
            (any(part.get("type") == "input_text" and SUBAGENT_TASK in part.get("text", "") for part in told),
             f"the helper was not given the task as text: {told}"),
            (not any(part.get("type") == "encrypted_content" for part in told),
             f"the helper was given the task as encrypted content: {told}"),
        ]
    if args.tool_search:
        def prologue(body: dict) -> str:
            # The bridge's catalog and reminder: what leads every request.
            return "\n".join(part.get("text", "") for item in body.get("input", [])[:3]
                             if isinstance(item, dict) and item.get("role") == "developer"
                             for part in item.get("content", []) if isinstance(part, dict))

        results = [item.get("output") for item in (backend.requests[1].get("input", [])
                                                    if len(backend.requests) >= 2 else [])
                   if isinstance(item, dict) and item.get("type") == "function_call_output"]
        checks += [
            (len(backend.requests) == 3, f"expected search, call and answer (3 requests), got {len(backend.requests)}"),
            (bool(backend.requests) and '"name":"tool_search"' in prologue(backend.requests[0]),
             "the catalog did not offer tool_search"),
            (not any(MCP_TOOL in prologue(body) for body in backend.requests),
             "the MCP tool joined the catalog at the top of the prompt"),
            (bool(backend.requests) and MCP_TOOL not in json.dumps(backend.requests[0]),
             "Codex sent the MCP tool before any search"),
            (any(f"mcp__{MCP_SERVER}.{MCP_TOOL}" in str(output) for output in results),
             f"the search result did not define the MCP tool: {results}"),
            ([body.get("metadata", {}).get("agent_iteration") for body in backend.requests] == ["1", "2", "3"],
             "each tool result should be a new agent iteration of the same turn"),
        ]
    if args.lite:
        # What the bridge told the model: its catalog is a developer message.
        told = [part.get("text", "") for item in (backend.requests[0].get("input", []) if backend.requests else [])
                if isinstance(item, dict) and item.get("role") == "developer"
                for part in item.get("content", []) if isinstance(part, dict)]
        checks += [
            (not any(isinstance(item, dict) and item.get("type") == "additional_tools"
                     for body in backend.requests for item in body.get("input", [])),
             "the Responses Lite tools item reached the backend"),
            (any('{"type":"custom","name":"exec"' in text for text in told),
             "the catalog the model saw did not list code mode's exec"),
            (any(text.startswith("You are Codex") for text in told),
             "Codex's instructions did not reach the model"),
        ]
    if gate:
        bridge_log = root / "bridge-home" / "bridge.log"
        said = bridge_log.read_text(encoding="utf-8", errors="replace") if bridge_log.exists() else ""
        said += _desktop_log(root)
        checks += [
            (gate.cut >= 3, f"expected the first connections cut, got {gate.cut}"),
            ("trying again for up to" in said and "again after" in said,
             "the bridge did not say it was trying again, or that it got through"),
            (not any(seen in output.lower() for seen in ("reconnecting", "disconnected", "bad gateway")),
             "Codex saw the drop"),
        ]
    failures = [message for ok, message in checks if not ok]

    print(output)
    print(f"--- {len(backend.requests)} upstream request(s), shell tool {backend.shell_tool or '-'}, "
          f"{time.monotonic() - started:.0f}s")
    for body in backend.requests:
        print("   ", body.get("model"), "reasoning_effort=", body.get("reasoning_effort"))
    print(f"    exit timezone {exit_zone}: Codex's context said {sorted(zones)} {sorted(dates)}")
    if failures:
        log = root / "bridge-home" / "bridge.log"
        if log.exists():
            print("--- bridge.log\n" + log.read_text(encoding="utf-8", errors="replace")[-4000:])
        if len(backend.requests) >= 2:
            tail = json.dumps(backend.requests[1])
            print("--- 2nd request tail\n" + tail[-1500:])
        for message in failures:
            print("FAIL:", message)
            if os.environ.get("GITHUB_ACTIONS") == "true":
                # The job log needs a sign-in; annotations do not.
                print("::error title=e2e::" + _annotation(message))
        return 1
    shutil.rmtree(root, ignore_errors=True)
    print("e2e ok")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print("::error title=e2e::" + _annotation(traceback.format_exc()[-3000:]))
        raise
    raise SystemExit(code)

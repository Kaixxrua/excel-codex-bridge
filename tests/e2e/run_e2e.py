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

``--images`` also attaches a picture (``codex exec -i``).  The backend must
only ever see it as a link, the same link every time, and fetches it from
there the way OpenAI would.  In the default ``local`` mode the link goes
through a stand-in for cloudflared (nothing leaves this computer); with
``--images remote`` it goes to an image host run next to the fake backend, and
with ``--images relay`` to an open one that only serves OpenAI's user agent.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse, StreamingResponse  # noqa: E402

from excel_codex_bridge import image_host  # noqa: E402
from helpers import write_webview_session  # noqa: E402

USER_PYTHONPATH = "e2e-user-pythonpath"
USER_CONFIG = '# e2e user config\nmodel = "gpt-5.5"\n\n[history]\npersistence = "none"\n'
CODEX_ARGS = [
    "exec", "--skip-git-repo-check", "-s", "danger-full-access",
    # A subcommand -c must not knock out the bridge's own overrides.
    "-c", "model_reasoning_effort=high",
    "Run the check.",
]
PYTHON = "python" if sys.platform == "win32" else "python3"
# Prints a marker only a real execution can produce, plus the PYTHONPATH Codex
# gave the command.  Works in bash, PowerShell and cmd alike.
PROBE = (
    f"{PYTHON} -c \"import os; print('bridge-e2e-' + str(6*7), "
    "'PP=[' + os.environ.get('PYTHONPATH', '') + ']')\""
)
IMAGE_TOKEN = "e2e-image-token-0123456789"
FAKE_TUNNEL = "https://e2e-fake-tunnel.trycloudflare.com"
# Stands in for cloudflared: notes how it was started, then prints what a quick
# tunnel prints once it is up.
FAKE_CLOUDFLARED = f"""
import json, os, sys, time
with open(os.environ["E2E_CLOUDFLARED_RECORD"], "a", encoding="utf-8") as out:
    out.write(json.dumps({{
        "argv": sys.argv[1:], "pid": os.getpid(), "home": os.environ.get("HOME"),
        "tunnel_env": sorted(key for key in os.environ if key.upper().startswith("TUNNEL_")),
    }}) + "\\n")
print("INF Requesting new quick Tunnel on trycloudflare.com...", flush=True)
print("INF |  {FAKE_TUNNEL}  |", flush=True)
print("INF Registered tunnel connection connIndex=0 location=e2e protocol=http2", flush=True)
while True:
    time.sleep(60)
"""
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


def image_urls(value) -> list[str]:
    """Every input_image URL anywhere in a request body."""
    if isinstance(value, list):
        return [url for item in value for url in image_urls(item)]
    if isinstance(value, dict):
        own = [value["image_url"]] if value.get("type") == "input_image" and "image_url" in value else []
        return own + [url for item in value.values() for url in image_urls(item)]
    return []


def fetch(url: str) -> bytes | None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    # What OpenAI fetches pictures as; the relay serves no one else.
    opener.addheaders = [("User-Agent", "OpenAI File Downloader")]
    try:
        with opener.open(url, timeout=10) as response:
            return response.read()
    except OSError:
        return None


def fake_cloudflared(root: Path) -> tuple[Path, Path]:
    """The stand-in's launcher, and the file it records its starts in."""
    script = root / "fake-cloudflared.py"
    script.write_text(FAKE_CLOUDFLARED, encoding="utf-8")
    if sys.platform == "win32":
        launcher = root / "cloudflared.cmd"
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        launcher = root / "cloudflared"
        launcher.write_text(f"#!{sys.executable}\n" + FAKE_CLOUDFLARED, encoding="utf-8")
        launcher.chmod(0o755)
    return launcher, root / "cloudflared-starts.jsonl"


def cloudflared_starts(record: Path) -> list[dict]:
    if not record.exists():
        return []
    return [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines() if line.strip()]


def tunnel_origin(record: Path) -> str | None:
    """Where the stand-in tunnel would send requests: the bridge's picture server."""
    for start in cloudflared_starts(record):
        argv = start["argv"]
        if "--url" in argv:
            return argv[argv.index("--url") + 1]
    return None


def alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def gone(pid: int, seconds: float = 15) -> bool:
    deadline = time.monotonic() + seconds
    while alive(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.2)
    return True


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
        # Like OpenAI, fetch every linked picture; `resolve` maps a link to where it is served.
        self.resolve = None
        self.fetched: list[bytes | None] = []
        counter = itertools.count(1)
        app = FastAPI()

        @app.post("/basispoints/api/responses")
        async def responses(request: Request):
            n = next(counter)
            body = await request.json()
            self.requests.append(body)
            raw = json.dumps(body)
            if self.resolve is not None:
                for url in image_urls(body.get("input")):
                    self.fetched.append(await asyncio.to_thread(fetch, self.resolve(url)))
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


def run_desktop(launcher, args, root: Path, webview: Path, project: Path, env: dict) -> tuple[str, list]:
    config = Path(env["CODEX_HOME"]) / "config.toml"
    config.write_bytes(USER_CONFIG.encode())
    port = free_port()
    command = [*launcher, "desktop", "--webview-dir", str(webview), "--model", args.model, "--port", str(port)]
    print("$", subprocess.list2cmdline(command), "&", flush=True)
    group = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
        else {"start_new_session": True}
    )
    log = root / "desktop.log"
    with open(log, "wb") as out:
        desktop = subprocess.Popen(
            command, cwd=project, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, **group
        )
    checks = []
    output = ""
    try:
        deadline = time.monotonic() + args.timeout
        while not healthy(port) and desktop.poll() is None and time.monotonic() < deadline:
            time.sleep(0.5)
        checks.append((healthy(port), "the desktop bridge did not come up"))
        enabled = config.read_text(encoding="utf-8")
        checks.append(("model_provider = 'excel-bridge'" in enabled, "config.toml was not pointed at the bridge"))
        codex = shutil.which("codex", path=env.get("PATH"))
        if codex and healthy(port):
            codex_env = dict(env)
            for key in ("NO_PROXY", "no_proxy"):
                codex_env[key] = ",".join(filter(None, [codex_env.get(key, ""), "127.0.0.1", "localhost"]))
            result = run([codex, *args.codex_args], cwd=project, env=codex_env, timeout=args.timeout)
            output = result.stdout
            checks.append((result.returncode == 0, f"codex exit code {result.returncode}"))
        else:
            checks.append((False, "codex not found on PATH" if not codex else "skipped codex"))
    finally:
        if desktop.poll() is None:
            if sys.platform == "win32":
                os.kill(desktop.pid, signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(desktop.pid, signal.SIGINT)
        try:
            code = desktop.wait(timeout=60)
        except subprocess.TimeoutExpired:
            desktop.kill()
            code = desktop.wait()
    desktop_output = log.read_text(encoding="utf-8", errors="replace")
    print("--- desktop window\n" + desktop_output)
    backup = config.with_name("config.toml.before-excel-codex")
    checks += [
        (code == 0, f"desktop exit code {code}"),
        (config.read_bytes() == USER_CONFIG.encode(), "config.toml was not restored exactly"),
        (backup.exists() and backup.read_bytes() == USER_CONFIG.encode(), "no exact backup of config.toml"),
    ]
    return output, checks


def _desktop_log(root: Path) -> str:
    log = root / "desktop.log"
    return log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default="gpt-5.6-sol-excel")
    parser.add_argument("--desktop", action="store_true", help="check `desktop` mode with a plain codex")
    parser.add_argument("--images", nargs="?", const="local", choices=["local", "remote", "relay"],
                        help="also attach a picture, passed on locally (default), through an image host, "
                             "or through a relay (an open image host)")
    parser.add_argument("--timeout", type=int, default=900, help="seconds for each long step")
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

    env = {key: value for key, value in os.environ.items()
           if not key.startswith("EXCEL_BRIDGE_IMAGE_") and key != "EXCEL_BRIDGE_RELAY_URL"}
    env.update(
        CODEX_HOME=str(root / "codex-home"),
        EXCEL_BRIDGE_HOME=str(root / "bridge-home"),
        GHCP_EXCEL_RESPONSES_URL=f"http://127.0.0.1:{port}/basispoints/api/responses",
        PYTHONPATH=USER_PYTHONPATH,
    )
    Path(env["CODEX_HOME"]).mkdir()
    args.codex_args = list(CODEX_ARGS)
    picture = png()
    record = root / "cloudflared-starts.jsonl"
    if args.images:
        (project / "picture.png").write_bytes(picture)
        # -i takes every argument after it, so it goes last.
        args.codex_args += ["-i", str(project / "picture.png")]
    if args.images == "local":
        cloudflared, record = fake_cloudflared(root)
        # A user's tunnel settings must not reach the bridge's cloudflared.
        env.update(EXCEL_BRIDGE_CLOUDFLARED=str(cloudflared), E2E_CLOUDFLARED_RECORD=str(record),
                   TUNNEL_ORIGIN_CERT=str(root / "no-such-cert.pem"))
        backend.resolve = lambda url: url.replace(FAKE_TUNNEL, tunnel_origin(record) or FAKE_TUNNEL, 1)
    elif args.images in {"remote", "relay"}:
        backend.resolve = lambda url: url
        host_port = free_port()
        relay = args.images == "relay"
        settings = image_host.Settings(
            public_url=f"http://127.0.0.1:{host_port}", tokens=() if relay else (IMAGE_TOKEN,),
            directory=root / "image-host", open_uploads=relay, uploads_per_hour=100 if relay else 0,
            fetchers=("OpenAI",) if relay else (),
        )
        host_server = uvicorn.Server(uvicorn.Config(
            image_host.create_app(settings), host="127.0.0.1", port=host_port, log_level="warning"
        ))
        threading.Thread(target=host_server.run, daemon=True).start()
        while not host_server.started:
            time.sleep(0.05)
        if relay:
            env.update(EXCEL_BRIDGE_IMAGE_HOST="relay", EXCEL_BRIDGE_RELAY_URL=settings.public_url)
        else:
            env.update(EXCEL_BRIDGE_IMAGE_HOST=settings.public_url, EXCEL_BRIDGE_IMAGE_TOKEN=IMAGE_TOKEN)
    started = time.monotonic()
    if args.desktop:
        output, checks = run_desktop(launcher, args, root, webview, project, env)
    else:
        output, checks = run_launcher(launcher, args, webview, project, env)
    server.should_exit = True

    upstream_model = args.model.removesuffix("-excel")
    checks += [
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
    if args.images:
        urls = [image_urls(body.get("input")) for body in backend.requests]
        hosted = sorted({url for request_urls in urls for url in request_urls})
        base = {"local": FAKE_TUNNEL, "remote": env.get("EXCEL_BRIDGE_IMAGE_HOST"),
                "relay": env.get("EXCEL_BRIDGE_RELAY_URL")}[args.images]
        announced = {"local": "Pictures: kept in memory", "remote": "Pictures: uploaded to",
                     "relay": "Pictures: sent through this project's relay"}[args.images]
        checks += [
            (announced in output + _desktop_log(root), f"the bridge did not announce {announced!r}"),
            (not any("data:" in url for request_urls in urls for url in request_urls),
             "an inline data: picture reached the backend"),
            (bool(urls) and all(len(request_urls) == 1 for request_urls in urls),
             f"expected the picture once in every request, got {[len(u) for u in urls]}"),
            (len(hosted) == 1 and hosted[0].startswith(base + "/i/"),
             f"expected one picture link under {base}, got {hosted}"),
            (len(backend.fetched) == len(backend.requests) and all(data == picture for data in backend.fetched),
             f"the backend could not fetch the picture: {[len(data or b'') for data in backend.fetched]}"),
        ]
    if args.images == "local":
        starts = cloudflared_starts(record)
        origin = tunnel_origin(record) or ""
        checks += [
            (len(starts) == 1, f"expected cloudflared to start once, got {len(starts)}"),
            (bool(starts) and starts[0]["argv"][:4] == ["tunnel", "--no-autoupdate", "--protocol", "http2"]
             and origin.startswith("http://127.0.0.1:"), f"cloudflared was started as {starts[:1]}"),
            (bool(starts) and not starts[0]["tunnel_env"], "TUNNEL_* settings reached cloudflared"),
            (bool(starts) and Path(starts[0]["home"] or "").is_relative_to(Path(env["EXCEL_BRIDGE_HOME"])),
             "cloudflared did not get its own home"),
            (bool(starts) and gone(starts[0]["pid"]), "cloudflared kept running after the bridge stopped"),
            (fetch(origin + "/") is None, "the picture server outlived the bridge"),
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

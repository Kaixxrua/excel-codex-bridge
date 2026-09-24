"""Command line: launch Codex through the bridge, or run the bridge alone."""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__, codex_config
from .session import SessionReader


def _print(message: str = "") -> None:
    print(message, flush=True)


def _describe_session(status: dict) -> tuple[bool, str]:
    if not status.get("configured"):
        error = status.get("error") or "no session found"
        return False, (
            f"No usable ChatGPT Excel session ({error}).\n"
            "  -> Open Excel, open the ChatGPT add-in pane and sign in, then run this again."
        )
    expires_at = status.get("expires_at")
    if status.get("expired"):
        return False, (
            "The cached ChatGPT Excel session has expired.\n"
            "  -> Open the ChatGPT add-in pane in Excel once so it refreshes, then run this again."
        )
    if isinstance(expires_at, (int, float)):
        local = _dt.datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d %H:%M")
        hours = (expires_at - time.time()) / 3600
        return True, f"ChatGPT Excel session found; expires {local} (in {hours:.1f} h)."
    return True, "ChatGPT Excel session found."


def _reader(args) -> SessionReader:
    webview_dir = getattr(args, "webview_dir", None)
    return SessionReader(webview_root=Path(webview_dir).expanduser() if webview_dir else None)


def _apply_proxy(args) -> None:
    if getattr(args, "proxy", None):
        os.environ["EXCEL_BRIDGE_PROXY"] = args.proxy


# ─── serve ────────────────────────────────────────────────────────────────────

def _exit_when_stdin_closes() -> None:
    """Launcher watchdog: the parent holds our stdin open for its lifetime."""

    def wait() -> None:
        try:
            while sys.stdin.buffer.read(4096):
                pass
        except (OSError, ValueError):
            pass
        os._exit(0)

    threading.Thread(target=wait, name="parent-watchdog", daemon=True).start()


def cmd_serve(args) -> int:
    import uvicorn

    from .server import create_app

    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        _print("Refusing to listen on a non-loopback address: this bridge is single-user and local-only.")
        return 2
    _apply_proxy(args)
    log_config = None
    if args.log_file:
        logging.basicConfig(
            filename=args.log_file,
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
    else:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.exit_with_stdin:
        _exit_when_stdin_closes()
    reader = _reader(args)
    if not args.log_file:
        _, message = _describe_session(reader.refresh(force=True))
        _print(message)
        _print(f"Listening on {codex_config.base_url(args.port)}  (Ctrl+C to stop)")
    uvicorn.run(
        create_app(reader),
        host=args.host,
        port=args.port,
        log_level="warning",
        log_config=log_config,
        access_log=not args.log_file,
    )
    return 0


# ─── status / print-config ────────────────────────────────────────────────────

def cmd_status(args) -> int:
    ok, message = _describe_session(_reader(args).refresh(force=True))
    _print(message)
    return 0 if ok else 1


def cmd_print_config(args) -> int:
    catalog = codex_config.write_catalog()
    _print(
        "# Put the three top-level keys ABOVE the first [table] of ~/.codex/config.toml,\n"
        "# and the [model_providers.excel-bridge] table anywhere below.\n"
    )
    _print(codex_config.config_snippet(args.port, catalog, args.model))
    return 0


# ─── codex launcher ───────────────────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _healthy(port: int) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/healthz", timeout=2) as response:
            return response.status == 200 and json.load(response).get("ok") is True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _find_codex(explicit: str | None) -> str | None:
    if explicit:
        return explicit if Path(explicit).exists() else shutil.which(explicit)
    return shutil.which("codex")


def _no_proxy_env(env: dict[str, str]) -> dict[str, str]:
    """Keep Codex's own HTTP proxy settings off the loopback hop."""
    for key in ("NO_PROXY", "no_proxy"):
        entries = [item.strip() for item in env.get(key, "").split(",") if item.strip()]
        for host in ("127.0.0.1", "localhost"):
            if host not in entries:
                entries.append(host)
        env[key] = ",".join(entries)
    return env


_PACKAGE_ROOT = str(Path(__file__).resolve().parent.parent)


def _frozen() -> bool:
    """Running from the PyInstaller build (the Windows release)."""
    return bool(getattr(sys, "frozen", False))


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _bridge_env(env: dict[str, str]) -> dict[str, str]:
    """The bridge child must import this package however the launcher was started."""
    if _frozen():
        return env
    entries = [item for item in env.get("PYTHONPATH", "").split(os.pathsep) if item]
    if not any(_same_path(item, _PACKAGE_ROOT) for item in entries):
        entries.insert(0, _PACKAGE_ROOT)
    env["PYTHONPATH"] = os.pathsep.join(entries)
    return env


def _codex_env(env: dict[str, str]) -> dict[str, str]:
    """Codex runs the user's commands; keep the start scripts' PYTHONPATH out of them."""
    entries = [
        item for item in env.get("PYTHONPATH", "").split(os.pathsep)
        if item and not _same_path(item, _PACKAGE_ROOT)
    ]
    if entries:
        env["PYTHONPATH"] = os.pathsep.join(entries)
    else:
        env.pop("PYTHONPATH", None)
    return _no_proxy_env(env)


def cmd_codex(args, codex_args: list[str]) -> int:
    _apply_proxy(args)
    codex = _find_codex(args.codex)
    if codex is None:
        _print(
            "Codex CLI was not found on PATH.\n"
            "  -> Install it (npm install -g @openai/codex) or pass --codex <path>."
        )
        return 127

    reader = _reader(args)
    ok, message = _describe_session(reader.refresh(force=True))
    _print(message)
    if not ok and not args.skip_session_check:
        return 1

    home = codex_config.state_dir()
    home.mkdir(parents=True, exist_ok=True)
    catalog = codex_config.write_catalog(home)
    log_file = home / "bridge.log"
    port = args.port or _free_port()

    serve_cmd = [
        *([sys.executable] if _frozen() else [sys.executable, "-m", "excel_codex_bridge"]),
        "serve", "--port", str(port), "--log-file", str(log_file), "--exit-with-stdin",
    ]
    if args.webview_dir:
        serve_cmd += ["--webview-dir", args.webview_dir]
    popen_kwargs: dict = {"stdin": subprocess.PIPE, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        # Keep console Ctrl+C aimed at Codex from also killing the bridge.
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    bridge = subprocess.Popen(serve_cmd, env=_bridge_env(os.environ.copy()), **popen_kwargs)
    try:
        deadline = time.monotonic() + 20
        while not _healthy(port):
            if bridge.poll() is not None or time.monotonic() > deadline:
                _print(f"The bridge did not start; see {log_file}")
                return 1
            time.sleep(0.2)
        _print(f"Bridge ready on {codex_config.base_url(port)} (log: {log_file}). Starting Codex...")

        command = codex_config.codex_command(
            codex, codex_config.codex_overrides(port, catalog, args.model), codex_args
        )
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            return subprocess.call(command, env=_codex_env(os.environ.copy()))
        finally:
            signal.signal(signal.SIGINT, previous)
    finally:
        if bridge.stdin:
            try:
                bridge.stdin.close()
            except OSError:
                pass
        try:
            bridge.wait(timeout=5)
        except subprocess.TimeoutExpired:
            bridge.kill()
            bridge.wait()


# ─── entry ────────────────────────────────────────────────────────────────────

def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--proxy",
        help="upstream proxy for bps.openai.com, e.g. http://127.0.0.1:7890 "
        "(default: HTTPS_PROXY / system proxy)",
    )
    common.add_argument(
        "--webview-dir",
        help="Excel WebView2 root (the ...\\Microsoft\\Office folder); for WSL or custom installs",
    )

    parser = argparse.ArgumentParser(
        prog="excel-codex",
        description="Run Codex on your own ChatGPT Excel add-in session, locally.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    codex = sub.add_parser(
        "codex",
        parents=[common],
        help="start the bridge and Codex together (default); extra args go to Codex",
    )
    codex.add_argument("--model", default=codex_config.DEFAULT_MODEL, help="Excel model alias")
    codex.add_argument("--port", type=int, default=0, help="bridge port (default: a free port)")
    codex.add_argument("--codex", help="path to the Codex executable")
    codex.add_argument(
        "--skip-session-check", action="store_true", help="start even if no session is found yet"
    )

    serve = sub.add_parser("serve", parents=[common], help="run only the bridge (for IDE / desktop Codex)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=codex_config.DEFAULT_PORT)
    serve.add_argument("--log-file", help=argparse.SUPPRESS)
    serve.add_argument("--exit-with-stdin", action="store_true", help=argparse.SUPPRESS)

    sub.add_parser("status", parents=[common], help="check the cached Excel session")

    config = sub.add_parser("print-config", help="print a config.toml snippet for `serve` mode")
    config.add_argument("--port", type=int, default=codex_config.DEFAULT_PORT)
    config.add_argument("--model", default=codex_config.DEFAULT_MODEL)
    return parser


def _started_by_double_click() -> bool:
    """True when Windows opened this console just for us, i.e. from Explorer."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        processes = (ctypes.c_uint32 * 4)()
        return ctypes.windll.kernel32.GetConsoleProcessList(processes, 4) == 1
    except (AttributeError, OSError):
        return False


def main(argv: list[str] | None = None) -> int:
    if argv is None and _started_by_double_click():
        # Explorer starts us in the install folder; keep Codex out of it, and
        # keep the window open long enough to read an error.
        if Path.cwd().resolve() == Path(sys.executable).resolve().parent:
            os.chdir(Path.home())
        code = _main(sys.argv[1:])
        if code:
            try:
                input("\nPress Enter to close this window...")
            except (EOFError, KeyboardInterrupt):
                pass
        return code
    return _main(list(sys.argv[1:] if argv is None else argv))


def _main(argv: list[str]) -> int:
    argv = list(argv)
    known = {"codex", "serve", "status", "print-config", "-h", "--help", "--version"}
    if not argv or argv[0] not in known:
        argv = ["codex", *argv]
    codex_args: list[str] = []
    if argv[0] == "codex" and "--" in argv:
        split = argv.index("--")
        argv, codex_args = argv[:split], argv[split + 1 :]
    args = _parser().parse_args(argv)
    if args.command == "serve":
        return cmd_serve(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "print-config":
        return cmd_print_config(args)
    return cmd_codex(args, codex_args)

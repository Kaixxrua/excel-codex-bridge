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

from . import __version__, codex_config, desktop_config, excel_signin
from .session import SessionReader


def _print(message: str = "") -> None:
    print(message, flush=True)


def _describe_session(status: dict) -> tuple[bool, str]:
    if not status.get("configured"):
        error = status.get("error") or "no session found"
        return False, (
            f"No usable ChatGPT Excel session ({error}).\n"
            "  -> Run `excel-codex login`, or open Excel's ChatGPT add-in pane and sign in."
        )
    expires_at = status.get("expires_at")
    if status.get("expired"):
        return False, (
            "The cached ChatGPT Excel session has expired.\n"
            "  -> Run `excel-codex login`, or open the ChatGPT add-in pane in Excel once."
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


# ─── automatic sign-in through Excel ──────────────────────────────────────────

def _signin(reader: SessionReader, args) -> excel_signin.ExcelSignIn:
    return excel_signin.ExcelSignIn(reader, enabled=False if getattr(args, "no_auto_signin", False) else None)


def _sign_in_now(signin: excel_signin.ExcelSignIn, status: dict) -> bool:
    _print(
        "Opening Excel with the ChatGPT add-in pane: sign in there (the first time, trust the add-in).\n"
        "  If no pane appears, click Home > Add-ins > ChatGPT. Waiting up to 10 minutes; Ctrl+C cancels."
    )
    try:
        fresh = signin.run(
            interactive=True,
            timeout=excel_signin.SIGN_IN_TIMEOUT_SECONDS,
            previous_expiry=excel_signin.expiry(status),
        )
    except OSError as exc:
        _print(f"Could not open Excel ({exc}).\n  -> Open Excel's ChatGPT pane and sign in yourself.")
        return False
    except KeyboardInterrupt:
        _print("Cancelled.")
        return False
    if fresh is None:
        _print("No new session showed up in time; run `excel-codex status` once you have signed in.")
        return False
    _print(_describe_session(fresh)[1])
    return True


def _ensure_session(reader: SessionReader, args) -> bool:
    """A usable session, signing in through Excel when there is none.

    A session that merely expires soon is left to the bridge's background
    keeper, so Codex starts at once.
    """
    status = reader.refresh(force=True)
    ok, message = _describe_session(status)
    _print(message)
    if ok:
        return True
    signin = _signin(reader, args)
    return signin.enabled and _sign_in_now(signin, status)


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
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        _print("Refusing to listen on a non-loopback address: this bridge is single-user and local-only.")
        return 2
    _apply_proxy(args)
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
    _run_bridge(reader, args, host=args.host, port=args.port, quiet=bool(args.log_file))
    return 0


SHUTDOWN_GRACE_SECONDS = 3


def _run_bridge(reader: SessionReader, args, *, host: str, port: int, quiet: bool) -> None:
    import uvicorn

    from .server import create_app

    keeper = excel_signin.SessionKeeper(_signin(reader, args))
    keeper.start()
    try:
        uvicorn.run(
            create_app(reader),
            host=host,
            port=port,
            log_level="warning",
            log_config=None,
            access_log=not quiet,
            # On Windows, a client that resets its connection can make asyncio lose
            # track of it (the proactor's shutdown() raises before the server is told),
            # and uvicorn would then wait for it forever on Ctrl+C, so `desktop` never
            # got to put config.toml back. Cap the wait.
            timeout_graceful_shutdown=SHUTDOWN_GRACE_SECONDS,
        )
    finally:
        keeper.stop()


# ─── status / print-config ────────────────────────────────────────────────────

def cmd_status(args) -> int:
    ok, message = _describe_session(_reader(args).refresh(force=True))
    _print(message)
    return 0 if ok else 1


def cmd_login(args) -> int:
    reader = _reader(args)
    status = reader.refresh(force=True)
    ok, message = _describe_session(status)
    _print(message)
    signin = _signin(reader, args)
    if not signin.enabled:
        _print("Signing in through Excel automatically needs Excel on Windows (and EXCEL_BRIDGE_AUTO_SIGNIN not 0).")
        return 0 if ok else 1
    if ok and not args.force and not excel_signin.needs_refresh(status):
        _print("Nothing to do; `excel-codex login --force` opens the ChatGPT pane anyway.")
        return 0
    return 0 if _sign_in_now(signin, status) else 1


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
    """Running from a PyInstaller release build."""
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
    if not _ensure_session(reader, args) and not args.skip_session_check:
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
    if args.no_auto_signin:
        serve_cmd.append("--no-auto-signin")
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


# ─── desktop app / IDE extension ──────────────────────────────────────────────

def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _once(action):
    lock = threading.Lock()
    done: list[bool] = []

    def run() -> None:
        with lock:
            if done:
                return
            done.append(True)
            try:
                action()
            except (OSError, UnicodeError) as exc:
                _print(f"Could not restore the Codex config: {exc}\n  -> Run `excel-codex desktop --off`.")

    return run


def _raise_interrupt(signum, frame) -> None:
    raise KeyboardInterrupt


def _undo_on_exit(undo):
    """Run ``undo`` however this window goes away; keep the result referenced."""
    for name in ("SIGTERM", "SIGBREAK", "SIGHUP"):
        if hasattr(signal, name):
            # uvicorn stops gracefully on these, then re-raises them into us.
            signal.signal(getattr(signal, name), _raise_interrupt)
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    # Closing the console window, logging off or shutting down.
    closing = {2, 5, 6}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    def handler(event):
        if event in closing:
            undo()
        return False

    ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True)
    return handler


def cmd_desktop(args) -> int:
    config = desktop_config.config_path()
    if args.off:
        try:
            changed = desktop_config.disable_file(config)
        except (OSError, UnicodeError) as exc:
            _print(f"Could not update {config}: {exc}")
            return 1
        _print(f"Restored {config}." if changed else f"Nothing to undo in {config}.")
        return 0

    _apply_proxy(args)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    reader = _reader(args)
    if not _ensure_session(reader, args) and not args.skip_session_check:
        return 1
    if not _port_free(args.port):
        _print(
            f"Port {args.port} is already in use (another `excel-codex desktop` or `serve`?).\n"
            "  -> Close it, or pass --port."
        )
        return 1
    catalog = codex_config.write_catalog()
    try:
        backup = desktop_config.enable_file(config, port=args.port, catalog=catalog, model=args.model)
    except (desktop_config.ConfigError, OSError, UnicodeError) as exc:
        _print(f"Could not update {config}: {exc}")
        return 1
    undo = _once(lambda: desktop_config.disable_file(config))
    keep = _undo_on_exit(undo) if not args.keep_config else None

    _print(f"Codex desktop app and IDE extension now use the Excel bridge ({args.model}).")
    _print(f"  Updated {config}" + (f"; the original is saved as {backup.name}" if backup else ""))
    profile = desktop_config.profile_override(config.read_text(encoding="utf-8-sig"))
    if profile:
        _print(f"  Note: your active profile '{profile}' sets its own model and may override this.")
    _print("  Restart the Codex desktop app (or reload the IDE window) to pick it up.")
    if args.keep_config:
        _print("  Keep this window open. `excel-codex desktop --off` puts your config back.")
    else:
        _print("  Keep this window open; closing it or pressing Ctrl+C puts your config back.")
    _print(f"Listening on {codex_config.base_url(args.port)}")
    try:
        _run_bridge(reader, args, host="127.0.0.1", port=args.port, quiet=False)
    except KeyboardInterrupt:
        pass
    finally:
        if not args.keep_config:
            undo()
            _print(f"Restored {config}.")
    del keep
    return 0


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

    auto = argparse.ArgumentParser(add_help=False)
    auto.add_argument(
        "--no-auto-signin",
        action="store_true",
        help="never open Excel to sign in or refresh the session (Windows)",
    )

    parser = argparse.ArgumentParser(
        prog="excel-codex",
        description="Run Codex on your own ChatGPT Excel add-in session, locally.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    codex = sub.add_parser(
        "codex",
        parents=[common, auto],
        help="start the bridge and Codex together (default); extra args go to Codex",
    )
    codex.add_argument("--model", default=codex_config.DEFAULT_MODEL, help="Excel model alias")
    codex.add_argument("--port", type=int, default=0, help="bridge port (default: a free port)")
    codex.add_argument("--codex", help="path to the Codex executable")
    codex.add_argument(
        "--skip-session-check", action="store_true", help="start even if no session is found yet"
    )

    desktop = sub.add_parser(
        "desktop",
        parents=[common, auto],
        help="route the Codex desktop app / IDE extension through the bridge while this runs",
    )
    desktop.add_argument("--model", default=codex_config.DEFAULT_MODEL, help="Excel model alias")
    desktop.add_argument("--port", type=int, default=codex_config.DEFAULT_PORT)
    desktop.add_argument("--off", action="store_true", help="only put the Codex config back, then exit")
    desktop.add_argument(
        "--keep-config", action="store_true", help="leave the config in place when this window closes"
    )
    desktop.add_argument(
        "--skip-session-check", action="store_true", help="start even if no session is found yet"
    )

    serve = sub.add_parser("serve", parents=[common, auto], help="run only the bridge (for IDE / desktop Codex)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=codex_config.DEFAULT_PORT)
    serve.add_argument("--log-file", help=argparse.SUPPRESS)
    serve.add_argument("--exit-with-stdin", action="store_true", help=argparse.SUPPRESS)

    sub.add_parser("status", parents=[common], help="check the cached Excel session")

    login = sub.add_parser(
        "login", parents=[common, auto], help="open Excel's ChatGPT pane to sign in or refresh (Windows)"
    )
    login.add_argument("--force", action="store_true", help="open the pane even if the session is fine")

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
    known = {"codex", "desktop", "serve", "status", "login", "print-config", "-h", "--help", "--version"}
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
    if args.command == "login":
        return cmd_login(args)
    if args.command == "desktop":
        return cmd_desktop(args)
    if args.command == "print-config":
        return cmd_print_config(args)
    return cmd_codex(args, codex_args)

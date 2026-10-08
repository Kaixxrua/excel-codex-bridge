"""Command line: launch Codex through the bridge, or run the bridge alone."""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import logging
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__, codex_config, desktop_config, excel_signin, excel_upstream, exit_timezone, images, updates
from . import image_generation
from . import self_update
from . import session
from . import upstream_routes
from .session import SessionReader


def _print(message: str = "") -> None:
    print(message, flush=True)


_SIGN_IN_ADVICE = {
    "auto": "Sign in to Codex with ChatGPT (`codex login`), or run `excel-codex login` / "
    "open Excel's ChatGPT add-in pane and sign in.",
    "codex": "Sign in to Codex with ChatGPT: `codex login`.",
    "excel": "Run `excel-codex login`, or open Excel's ChatGPT add-in pane and sign in.",
}


def _describe_session(status: dict) -> tuple[bool, str]:
    advice = _SIGN_IN_ADVICE.get(status.get("login") or "auto", _SIGN_IN_ADVICE["auto"])
    if not status.get("configured"):
        error = status.get("error") or "no session found"
        return False, f"No usable ChatGPT sign-in ({error}).\n  -> {advice}"
    source = status.get("source")
    name = session.NAMES.get(source or "", "the ChatGPT session")
    if status.get("expired"):
        return False, f"{name[0].upper()}{name[1:]} has expired.\n  -> {advice}"
    where = f" ({status.get('codex_auth')})" if source == "codex" else ""
    message = f"Using {name}{where}"
    expires_at = status.get("expires_at")
    if isinstance(expires_at, (int, float)):
        local = _dt.datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d %H:%M")
        hours = (expires_at - time.time()) / 3600
        message += f"; expires {local} (in {hours:.1f} h)"
    message += "."
    notes = status.get("notes")
    if source == "excel" and status.get("login") == "auto" and isinstance(notes, str) and notes:
        message += f"\n  Not using Codex's sign-in: {notes.removeprefix('codex: ')}"
    return True, message


def _reader(args, *, login: str | None = None) -> SessionReader:
    webview_dir = getattr(args, "webview_dir", None)
    requested_login = login or getattr(args, "login", None)
    if upstream_routes.selected(args) == "codex" and login is None:
        if requested_login == "excel":
            raise ValueError("--route codex uses --login codex; select --route excel for an Excel session.")
        requested_login = "codex"
    return SessionReader(
        webview_root=Path(webview_dir).expanduser() if webview_dir else None,
        login=requested_login,
    )


def _apply_proxy(args) -> None:
    if getattr(args, "proxy", None):
        os.environ["EXCEL_BRIDGE_PROXY"] = args.proxy
    # The bridge child started by `codex` inherits these.
    if getattr(args, "timezone", None):
        os.environ[exit_timezone.MODE_ENV] = args.timezone
    if getattr(args, "image_model", None):
        os.environ[image_generation.MODEL_ENV] = args.image_model


def _write_catalog(directory: Path | None = None) -> Path:
    return codex_config.write_catalog(directory)


def _route_catalog(args, reader: SessionReader, directory: Path | None = None) -> Path:
    if upstream_routes.selected(args) == "excel":
        return _write_catalog(directory)
    from .native_upstream import CATALOG_NAME, NativeBridge, RouteError
    from .server import build_upstream_client

    async def fetch():
        bridge = NativeBridge(reader, build_upstream_client)
        try:
            return await bridge.catalog()
        finally:
            await bridge.aclose()

    payload = asyncio.run(fetch())
    model = getattr(args, "model", None)
    if model is not None and model not in {m["slug"] for m in payload["models"]}:
        raise RouteError(f"Model {model!r} is not in this account's Codex catalog. Excel aliases and their context limits require --route excel.")
    return codex_config.write_catalog(directory, payload=payload, name=CATALOG_NAME)


def _route_notice(args) -> None:
    route = upstream_routes.selected(args)
    endpoint = "chatgpt.com/backend-api/codex" if route == "codex" else "bps.openai.com"
    _print(f"Upstream route: {route} ({endpoint}); fixed for this process.")


def _refresh_catalog() -> None:
    try:
        _write_catalog()
    except OSError as exc:
        logging.getLogger(__name__).warning("could not update the model list for Codex: %s", exc)


def _image_model(value: str) -> str:
    value = value.strip()
    if not image_generation.valid_model(value):
        raise argparse.ArgumentTypeError(f"{value!r} is not an image model name")
    return value


def _pictures_line(args=None) -> str:
    if args is not None and upstream_routes.selected(args) == "codex":
        return "Native Codex tools and image inputs are forwarded unchanged. Standalone image generation is unavailable on this HTTP adapter."
    try:
        drawing = f"draws with {image_generation.model()} (--image-model picks another)."
    except image_generation.Refused as exc:
        drawing = str(exc)
    return f"{images.DESCRIPTION}\nImage tool: {drawing}"


# ─── update check ─────────────────────────────────────────────────────────────

_update_shown = threading.Event()


def _update_check() -> updates.UpdateCheck | None:
    if not updates.enabled():
        return None
    return updates.UpdateCheck(codex_config.state_dir() / updates.CACHE_NAME)


def _announce_update(release: updates.Release | None, stream=None) -> None:
    if release is None:
        return
    stream = stream or sys.stdout
    text = updates.notice(release, installs_itself=self_update.installs_itself())
    try:
        print(text, file=stream, flush=True)
    except UnicodeEncodeError:  # output redirected to a file in a narrow code page
        print(text.encode("ascii", "replace").decode("ascii"), file=stream, flush=True)
    _update_shown.set()


def _watch_for_updates() -> None:
    """For the windows that stay open: say so whenever a newer release comes out."""
    check = _update_check()
    if check is not None:
        updates.watch(check, _announce_update)


def cmd_update(args) -> int:
    app = self_update.install_dir()
    if args.launcher:
        # excel-codex-desktop.cmd, before it starts the bridge: never in its way.
        if app is None or not self_update.enabled():
            return 0
        try:
            return self_update.installer(app, codex_config.state_dir()).launcher()
        except Exception as exc:
            _print(f"Could not check for updates ({type(exc).__name__}: {exc}); starting the current version.")
            return 0
    if app is not None:
        return self_update.installer(app, codex_config.state_dir()).by_hand()
    release = updates.fetch_latest()
    if release is None:
        _print("Could not read the latest release from GitHub (offline, or behind a proxy?).")
        return 1
    if not updates.is_newer(release.version):
        _print(f"You have the newest release ({__version__}).")
        return 0
    _announce_update(release)
    if _frozen():
        _print("  Only the Windows package installs updates itself; download this one and replace your folder.")
    else:
        _print("  Running from the source code: `git pull` updates it.")
    return 0


# ─── automatic sign-in through Excel ──────────────────────────────────────────

def _signin(reader: SessionReader, args) -> excel_signin.ExcelSignIn:
    disabled = upstream_routes.selected(args) == "codex" or getattr(args, "no_auto_signin", False)
    return excel_signin.ExcelSignIn(reader, enabled=False if disabled else None)


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
        # A config from `print-config` points at this file; keep its model list this release's.
        if upstream_routes.selected(args) == "codex":
            _route_catalog(args, reader)
        else:
            _refresh_catalog()
        _route_notice(args)
        _, message = _describe_session(reader.refresh(force=True))
        _print(message)
        _print(_pictures_line(args))
        _print(f"Listening on {codex_config.base_url(args.port)}  (Ctrl+C to stop)")
        _watch_for_updates()
    _run_bridge(reader, args, host=args.host, port=args.port, quiet=bool(args.log_file))
    return 0


SHUTDOWN_GRACE_SECONDS = 3


def _keep_native_calls() -> None:
    home = codex_config.state_dir()
    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    excel_upstream.keep_native_calls_in(home / "tool-calls.sqlite3")


def _quiet_upgrades(record: logging.LogRecord) -> bool:
    """uvicorn warns about every WebSocket try, which the app turns away on purpose."""
    message = record.getMessage()
    return not (message == "Unsupported upgrade request." or message.startswith("No supported WebSocket library"))


def _run_bridge(reader: SessionReader, args, *, host: str, port: int, quiet: bool) -> None:
    import uvicorn

    from .server import create_app

    if upstream_routes.selected(args) == "excel":
        _keep_native_calls()
    keeper = excel_signin.SessionKeeper(_signin(reader, args))
    keeper.start()
    try:
        config = uvicorn.Config(
            create_app(reader, route=upstream_routes.selected(args)),
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
            # Codex tries a WebSocket first; the app answers the upgrade with 426 (HTTP only).
            ws="none",
        )
        logging.getLogger("uvicorn.error").addFilter(_quiet_upgrades)
        if not quiet:
            # log_level hides uvicorn's startup chatter but also the request lines, which
            # show whether Codex reaches the bridge at all (method, path and status only).
            logging.getLogger("uvicorn.access").setLevel(logging.INFO)
        # The rest is what uvicorn.run does: Ctrl+C is re-raised after shutdown.
        server = uvicorn.Server(config)
        try:
            server.run()
        except KeyboardInterrupt:
            pass
        if not server.started:
            sys.exit(3)  # could not start, e.g. port taken
    finally:
        keeper.stop()


# ─── status / print-config ────────────────────────────────────────────────────

def cmd_status(args) -> int:
    _route_notice(args)
    ok, message = _describe_session(_reader(args).refresh(force=True))
    _print(message)
    if upstream_routes.selected(args) == "codex":
        _print("Login status is not an inference check. Run `excel-codex check-route` to verify a tool call and its continuation (at most 2 requests).")
    return 0 if ok else 1


def cmd_check_route(args) -> int:
    if upstream_routes.selected(args) != "codex":
        raise ValueError("This check validates native Codex. Use --route codex; legacy Excel retries are not a controlled channel comparison.")
    from . import route_check
    from .server import build_upstream_client
    import uuid

    _apply_proxy(args)
    receipt = codex_config.state_dir() / "route-checks" / (uuid.uuid4().hex + ".json")
    _print(f"Checking native tool calling and continuation: at most 2 inference requests. Receipt: {receipt}")
    report = asyncio.run(route_check.run(_reader(args), build_upstream_client, model=args.model,
                                        effort=args.effort, receipt=receipt))
    _print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["protocol_verified"] else 1


def cmd_login(args) -> int:
    if upstream_routes.selected(args) == "codex":
        codex = _find_codex(None)
        if codex is None:
            _print("Codex CLI was not found. Install Codex, then run codex login.")
            return 127
        return subprocess.call([codex, "login"], env=_codex_env(os.environ.copy()))
    reader = _reader(args, login="excel")
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
    _apply_proxy(args)
    catalog = _route_catalog(args, _reader(args))
    _print(
        "# Put the three top-level keys ABOVE the first [table] of ~/.codex/config.toml,\n"
        "# and the [model_providers.excel-bridge] table anywhere below.\n"
    )
    _print(codex_config.config_snippet(args.port, catalog, args.model,
                                      excel=upstream_routes.selected(args) == "excel"))
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
    check = _update_check()
    update = updates.in_background(check) if check is not None else None
    catalog = _route_catalog(args, reader, home)
    log_file = home / "bridge.log"
    port = args.port or _free_port()

    serve_cmd = [
        *([sys.executable] if _frozen() else [sys.executable, "-m", "excel_codex_bridge"]),
        "serve", "--port", str(port), "--log-file", str(log_file), "--exit-with-stdin",
        "--route", upstream_routes.selected(args),
    ]
    if args.webview_dir:
        serve_cmd += ["--webview-dir", args.webview_dir]
    if args.login:
        serve_cmd += ["--login", args.login]
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
        shared = codex_config.codex_signed_in()
        if shared:
            _move_bridge_threads(desktop_config.codex_home())
        _route_notice(args)
        _print(_pictures_line(args))
        _print(f"Bridge ready on {codex_config.base_url(port)} (log: {log_file}). Starting Codex...")

        command = codex_config.codex_command(
            codex, codex_config.codex_overrides(port, catalog, args.model, shared=shared,
                                               excel=upstream_routes.selected(args) == "excel"), codex_args
        )
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            return subprocess.call(command, env=_codex_env(os.environ.copy()))
        finally:
            signal.signal(signal.SIGINT, previous)
            # Only once Codex is done: its screen would hide the notice, and
            # stderr keeps it out of `codex exec` output that scripts read.
            if update is not None:
                _announce_update(update(0), stream=sys.stderr)
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


# The desktop app reads its config and model list only at startup, and a
# conversation it has open keeps the route it was opened with: to the bridge,
# gone once this window is.
_REOPEN_AFTER_RESTORE = (
    "  Now fully quit the Codex desktop app and open it again (Windows: right-click its tray icon > Quit;\n"
    "  closing its window leaves it running), and reload IDE windows that use Codex. Until then the\n"
    "  conversations opened meanwhile still go to the bridge and fail with \"connection refused\"\n"
    "  (os error 10061), and the app still offers the bridge's models (1M ones fail without it)."
)
_STILL_RUNNING = "  Codex is still running right now: quit it as above before carrying on."
_RUNNING_AT_START = (
    "  Codex is running right now: until it is fully quit (Windows: right-click its tray icon > Quit)\n"
    "  and opened again, it keeps the model list it started with, such as an older bridge's without\n"
    "  the 1M and 6-Sol models."
)
_QUIT_WHEN_DONE = (
    "  When done, fully quit Codex too (Windows: its tray icon > Quit) before using it without the\n"
    "  bridge: conversations opened meanwhile keep going to this window until then."
)
_SHARED = (
    "  Conversations are shared with Codex's official sign-in: earlier ones carry on through the\n"
    "  bridge, and ones started now carry on without it once it is off (1M models: pick another)."
)
_SEPARATE = (
    "  Codex is not signed in, so the bridge is a provider of its own: conversations started now\n"
    "  are listed only while it is on. `codex login` once to share them with the official sign-in."
)
# Signed in with ChatGPT, Codex waits on chatgpt.com for these when a conversation opens
# (apps, up to 30 s) and on every turn (plugin suggestions, 5 s).
_APPS_OFF = (
    "  Codex's apps and plugin suggestions are off while this runs: they wait on chatgpt.com, so a\n"
    "  slow proxy kept conversations loading. --keep-apps leaves them on."
)
_APPS_LEFT_ON = (
    "  Codex's apps and plugin suggestions stay on: config.toml sets [features] in a way this cannot\n"
    "  change. Through a slow proxy they can keep conversations loading for up to 30 s."
)


def _move_bridge_threads(home: Path) -> None:
    """Move the bridge's own conversations into the shared list, if Codex is not running.

    With the bridge off, Codex cannot open them otherwise ("Model provider
    `excel-bridge` not found").  Conversations an earlier excel-codex moved get
    OpenAI's models in their file too.  EXCEL_BRIDGE_AUTO_MIGRATE=0 leaves that
    to `excel-codex threads migrate`.
    """
    from . import codex_threads

    record_dir = codex_config.state_dir()
    try:
        count = len(codex_threads.bridge_threads(home, record_dir))
        unfinished = len(codex_threads.unfinished_threads(home, record_dir))
        if not count and not unfinished:
            return
        if os.environ.get("EXCEL_BRIDGE_AUTO_MIGRATE", "").strip() == "0":
            if count:
                _print(f"  {count} conversation(s) under the bridge's own provider open only while it is on:\n"
                       "  `excel-codex threads migrate` moves them into the shared list.")
            if unfinished:
                _print(f"  {unfinished} conversation(s) moved by an earlier excel-codex still name the bridge's models in\n"
                       "  their file: `excel-codex threads migrate` finishes them.")
            return
        if codex_threads.codex_running():
            if count:
                _print(f"  {count} conversation(s) under the bridge's own provider open only while it is on. They move\n"
                       "  into the shared list when this starts while Codex (desktop app, IDE, `codex` in a\n"
                       "  terminal) is fully quit, or with `excel-codex threads migrate` then.")
            if unfinished:
                _print(f"  {unfinished} conversation(s) moved by an earlier excel-codex still name the bridge's models in\n"
                       "  their file. They are finished when this starts while Codex is fully quit, or with\n"
                       "  `excel-codex threads migrate` then.")
            return
        result = codex_threads.migrate(home, record_dir)
    except (codex_threads.Refused, OSError, sqlite3.Error) as exc:
        _print(f"  Could not move the bridge's own conversations into the shared list: {exc}")
        return
    if result.threads:
        _print(f"  Moved {len(result.threads)} conversation(s) from the bridge's own provider into the shared list,\n"
               "  so they open with the bridge off too (`excel-codex threads undo` puts them back).")
    if result.finished:
        _print(f"  Finished {len(result.finished)} conversation(s) moved by an earlier excel-codex: with the bridge\n"
               "  off they carry on with OpenAI's models now.")
    if _had_1m(result):
        _print(_1M_MOVED)
    if result.left:
        _print(f"  {len(result.left)} conversation(s) could not be moved; `excel-codex threads` lists them.")


_1M_MOVED = ("  1M conversations among them carry on with the same model's official version; pick\n"
             "  a 1M model again while the bridge is on.")


def _had_1m(result) -> bool:
    return any((thread.model or "").endswith(excel_upstream.LONG_CONTEXT_SUFFIX)
               for thread in [*result.threads, *result.finished])


def cmd_desktop(args) -> int:
    config = desktop_config.config_path()
    if args.off:
        try:
            changed = desktop_config.disable_file(config)
        except (OSError, UnicodeError) as exc:
            _print(f"Could not update {config}: {exc}")
            return 1
        _print(f"Restored {config}." if changed else f"Nothing to undo in {config}.")
        if changed:
            _print(_REOPEN_AFTER_RESTORE)
            _say_if_codex_runs()
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
    catalog = _route_catalog(args, reader)
    shared = codex_config.codex_signed_in(desktop_config.codex_home())
    try:
        backup = desktop_config.enable_file(
            config, port=args.port, catalog=catalog, model=args.model, shared=shared,
            quiet_features=not args.keep_apps and upstream_routes.selected(args) == "excel",
            excel=upstream_routes.selected(args) == "excel",
        )
    except (desktop_config.ConfigError, OSError, UnicodeError) as exc:
        _print(f"Could not update {config}: {exc}")
        return 1
    restore_config = _once(lambda: desktop_config.disable_file(config))
    # The Windows timezone keeper, once started.
    timezone: list = []

    def undo() -> None:
        # However this window goes away: the config, then the Windows timezone.
        if not args.keep_config:
            restore_config()
        for keeper in timezone:
            _put_back_windows_timezone(keeper)

    keep = _undo_on_exit(undo)

    _print(f"Codex desktop app and IDE extension now use the Excel bridge {__version__} "
           f"({codex_config.codex_model(args.model)}).")
    _route_notice(args)
    _print(f"  Updated {config}" + (f"; the original is saved as {backup.name}" if backup else ""))
    text = config.read_text(encoding="utf-8-sig")
    if not args.keep_apps and upstream_routes.selected(args) == "excel":
        _print(_APPS_OFF if desktop_config.features_quiet(text) else _APPS_LEFT_ON)
    profile = desktop_config.profile_override(text)
    if profile:
        _print(f"  Note: your active profile '{profile}' sets its own model and may override this.")
    _print("  Fully quit and reopen the Codex desktop app (or reload the IDE window) to pick it up.")
    _say_if_codex_runs(_RUNNING_AT_START)
    _print(_SHARED if shared else _SEPARATE)
    if shared:
        _move_bridge_threads(desktop_config.codex_home())
    if args.keep_config:
        _print("  Keep this window open. `excel-codex desktop --off` puts your config back.")
    else:
        _print("  Keep this window open; closing it or pressing Ctrl+C puts your config back.")
    _print(_QUIT_WHEN_DONE)
    _print(_pictures_line(args))
    _print(f"Listening on {codex_config.base_url(args.port)}")
    _watch_for_updates()
    if upstream_routes.selected(args) == "excel":
        timezone.extend(filter(None, [_keep_windows_timezone()]))
    try:
        _run_bridge(reader, args, host="127.0.0.1", port=args.port, quiet=False)
    except KeyboardInterrupt:
        pass
    finally:
        undo()
        if not args.keep_config:
            _print(f"Restored {config}.")
            _print(_REOPEN_AFTER_RESTORE)
            _say_if_codex_runs()
    del keep
    return 0


def _say_if_codex_runs(message: str = _STILL_RUNNING) -> None:
    from . import codex_threads

    if codex_threads.codex_seen():
        _print(message)


# ─── restore: back to Codex's own setup ───────────────────────────────────────

def cmd_restore(args) -> int:
    """Codex as it was before the bridge: config.toml, the bridge's own conversations, the Windows timezone."""
    config = desktop_config.config_path()
    try:
        restored = desktop_config.official_file(config)
    except (OSError, UnicodeError) as exc:
        _print(f"Could not update {config}: {exc}")
        return 1
    if restored.changed:
        _print(f"Codex is back on its own setup: {config}")
    elif restored.stuck:
        _print(f"The bridge is still set up in {config}:")
    elif restored.kept:
        _print(f"Nothing of the bridge's in {config}.")
    else:
        _print(f"Nothing of the bridge's in {config}; Codex is on its own setup there.")
    if restored.blocks:
        _print("  Took out what `excel-codex desktop` had put in, and put back the lines it had set aside.")
    if restored.took_out:
        _print("  Commented out the bridge's settings put in some other way (such as a pasted `print-config`):")
        for line in restored.took_out:
            _print(f"    {line}")
        if restored.backup:
            _print(f"  The file as it was is saved as {restored.backup.name}.")
    if restored.stuck:
        _print("  These are the bridge's, but commenting them out would break the file; delete them by hand:")
        for line in restored.stuck:
            _print(f"    {line}")
    if restored.kept:
        _print("  Left as they are: not the bridge's (a relay's?), though Codex's own setup has none of them:")
        for line in restored.kept:
            _print(f"    {line}")
    _restore_conversations(desktop_config.codex_home())
    _restore_windows_timezone()
    if restored.changed:
        _print(_REOPEN_AFTER_RESTORE)
        _say_if_codex_runs()
    return 1 if restored.stuck else 0


def _restore_conversations(home: Path) -> None:
    """The bridge's own conversations open only through it: into Codex's own list, where it can."""
    from . import codex_threads

    if codex_config.codex_signed_in(home):
        _move_bridge_threads(home)
        return
    try:
        count = len(codex_threads.bridge_threads(home, codex_config.state_dir()))
    except (codex_threads.Refused, OSError, sqlite3.Error):
        return
    if count:
        _print(f"  {count} conversation(s) are filed under the bridge's own provider, which Codex cannot open\n"
               "  without it. Once Codex is signed in (`codex login`) and fully quit, `excel-codex threads migrate`\n"
               "  moves them into its own list.")


def _restore_windows_timezone() -> None:
    if sys.platform != "win32":
        return
    from . import system_timezone

    try:
        original = system_timezone.restore()
    except system_timezone.Refused as exc:
        _print(f"  Windows timezone: could not put it back ({exc}).\n  -> Run `excel-codex timezone restore`.")
        return
    if original:
        _print(f"  Windows timezone: put back {original}.")


# ─── timezone ─────────────────────────────────────────────────────────────────

def _redacted(url: str | None) -> str:
    from . import system_timezone

    return system_timezone.redacted(url)


# ASCII only: piped on Windows, output takes the ANSI code page.
_CODEX_NEEDS_CHATGPT = (
    "  Codex itself reaches chatgpt.com through this proxy too (sign-in, apps, plugins): while it\n"
    "  cannot, new tasks can hang on \"starting\" and conversations on loading. Pick a steady proxy\n"
    "  node for chatgpt.com and auth.openai.com.")


def _describe_sync(result: dict) -> str:
    from . import system_timezone

    status = result.get("status")
    if status == "error":
        text = f"Error: {result.get('error')}"
        return f"{text}\n{_CODEX_NEEDS_CHATGPT}" if result.get("unreachable") else text
    if status == "skipped":
        return f"Skipped: {result.get('reason')}"
    windows = result.get("windows_timezone")
    exit_windows = result.get("exit_windows_timezone") or windows
    before = result.get("previous_timezone")
    country = result.get("exit_country")
    text = (f"{result.get('route')} -> {result.get('exit_host')} via {_redacted(result.get('proxy'))}: "
            f"exit {result.get('exit_ip')}" + (f" (Cloudflare: {country})" if country else "")
            + f" is in {result.get('iana_timezone')}" + (f" ({exit_windows})" if exit_windows else ""))
    if status == "updated" and result.get("reverted"):
        why = ("Windows \"Set time zone automatically\" is on and changes it back: turn it off in\n"
               "  Settings > Time & language > Date & time" if result.get("automatic")
               else "Something changed it back (Windows \"Set time zone automatically\", or another program)")
        return (f"{text}\n  Windows timezone had gone back to {before}; set {windows} again.\n"
                f"  {why}, or use --timezone off. Later changes back are put right without a message.")
    if status == "updated":
        return f"{text}\n  Windows timezone changed from {before} to {windows}."
    if status == "waiting":
        return (f"{text}\n  Windows stays on {windows}: the proxy's exit moves between places, and another\n"
                f"  exit's timezone is taken once it holds for {system_timezone.SETTLE_CHECKS} checks in a row.")
    if status == "unchanged":
        return f"{text}\n  Windows timezone is already {before}."
    here = before or _dt.datetime.now().astimezone().strftime("UTC%z")
    return f"{text}\n  This computer: {here}."


def _when(value: object) -> str:
    try:
        return _dt.datetime.fromisoformat(str(value)).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return "?"


def cmd_timezone(args) -> int:
    from . import system_timezone

    if args.action == "restore":
        try:
            original = system_timezone.restore()
        except system_timezone.Refused as exc:
            _print(f"Could not restore it: {exc}")
            return 1
        _print(f"Windows timezone is {original} again." if original
               else "Nothing to restore: the Windows timezone was not changed by excel-codex.")
        return 0
    if args.action == "sync":
        try:
            result = system_timezone.sync(probe=args.probe)
        except Exception as exc:  # noqa: BLE001 - network, proxy, tzutil
            _print(f"Error: {exc}")
            return 1
        _print(_describe_sync(result))
        return 0

    mode = "auto" if exit_timezone.enabled() else "off"
    _print(f"Timezone matching: {mode}")
    if mode == "auto":
        _print("  Requests through the bridge carry the proxy exit's timezone and date, looked up from the\n"
               "  exit IP by ipwho.is, ipapi.co, get.geojs.io or api.ip.sb, whichever first agrees with\n"
               "  Cloudflare on the exit's country. --timezone off (or EXCEL_BRIDGE_TIMEZONE=off) turns it off.")
        if sys.platform == "win32":
            _print("  `excel-codex desktop` (excel-codex-desktop.cmd) also keeps the Windows timezone on Codex's\n"
                   "  exit while its window is open, for Codex's official ChatGPT sign-in too, and puts it back\n"
                   "  when the window closes.")
    original = system_timezone.original_zone() if sys.platform == "win32" else None
    if original:
        _print(f"  Before the first change: {original} (`excel-codex timezone restore` puts it back).")
    last = system_timezone.last_result()
    if last:
        _print(f"Last Windows sync {_when(last.get('checked_at'))}: {_describe_sync(last)}")
    try:
        result = system_timezone.sync(probe=True)
    except Exception as exc:  # noqa: BLE001 - network, proxy
        _print(f"Now: {exc}")
        return 1
    _print(f"Now: {_describe_sync(result)}")
    return 0


def _keep_windows_timezone():
    """``desktop`` on Windows: the system timezone follows Codex's exit while the window is open."""
    if sys.platform != "win32" or not exit_timezone.enabled():
        return None
    from . import system_timezone

    _print("Windows timezone: kept on Codex's proxy exit while this window is open and put back when it\n"
           "  closes (--timezone off leaves it alone). Times in this window stay in the timezone it\n"
           "  started in.")
    if system_timezone.automatic_timezone():
        _print("  Windows \"Set time zone automatically\" is on and may change it back; turn it off in\n"
               "  Settings > Time & language > Date & time.")
    return system_timezone.Keeper(lambda result: _print(f"Windows timezone: {_describe_sync(result)}")).start()


def _put_back_windows_timezone(keeper) -> None:
    try:
        original = keeper.put_back()
    except Exception as exc:  # noqa: BLE001 - tzutil
        _print(f"Windows timezone: could not put it back ({exc}).\n  -> Run `excel-codex timezone restore`.")
        return
    if original:
        _print(f"Windows timezone: put back {original}.")


# ─── threads ──────────────────────────────────────────────────────────────────

_REOPEN_FOR_LIST = "  Open Codex again (or reload the IDE window) to see the new list."
_QUIT_CODEX = (
    "Nothing was changed: Codex is running. Fully quit the Codex desktop app, IDE windows that use\n"
    "Codex, and `codex` in any terminal first: Codex puts back what changes while it runs."
)


def _thread_line(thread) -> str:
    title = " ".join((thread.title or "").split()) or thread.id
    title = title if len(title) <= 60 else title[:59] + "…"
    return f"    {title}" + (f"  ({thread.model})" if thread.model else "")


def _providers_line(counts: dict[str, int]) -> str:
    listed = ", ".join(f"`{name}` ({count})" for name, count in counts.items())
    return f"  Codex files conversations under: {listed or 'nothing yet'} (names are case-sensitive)."


def _other_providers(home: Path) -> None:
    """Point at conversations under providers other than ``openai`` and the bridge's own."""
    from . import codex_threads

    try:
        counts = codex_threads.providers(home)
    except (codex_threads.Refused, OSError, sqlite3.Error):
        return
    others = {name: count for name, count in counts.items()
              if name and name not in (codex_config.OPENAI_PROVIDER_ID, codex_config.PROVIDER_ID)}
    if others:
        listed = ", ".join(f"`{name}` ({count})" for name, count in others.items())
        # ASCII only: piped on Windows, output takes the ANSI code page.
        _print(f"Also filed under other providers: {listed}. If Codex cannot open those (\"Model provider\n"
               "`<provider>` not found\"), `excel-codex threads migrate --from <provider>` moves them under `openai`\n"
               "(Codex quit).")


def _threads_from(action: str | None, source: str, home: Path, record_dir: Path) -> int:
    """``threads [migrate] --from <provider>``: conversations under another provider."""
    from . import codex_threads

    if source == codex_config.OPENAI_PROVIDER_ID:
        _print("Conversations under `openai` are in the list Codex's official sign-in and the bridge share already.")
        return 0
    try:
        threads = codex_threads.threads_under(home, source)
        counts = {} if threads else codex_threads.providers(home)
        if action == "migrate" and threads:
            # Not signed in: `migrate` refuses first, saying why.
            if codex_config.codex_signed_in(home) and codex_threads.codex_running():
                _print(_QUIT_CODEX)
                return 1
            result = codex_threads.migrate(home, record_dir, source=source)
    except (codex_threads.Refused, OSError, sqlite3.Error) as exc:
        _print(f"Nothing was changed: {exc}")
        return 1

    if not threads:
        _print(f"No conversations are filed under `{source}`" + ("; nothing to move." if action == "migrate" else "."))
        _print(_providers_line(counts))
        return 0
    if action != "migrate":
        _print(f"{len(threads)} conversation(s) are filed under `{source}`:")
        for thread in threads[:10]:
            _print(_thread_line(thread))
        if len(threads) > 10:
            _print(f"    ... and {len(threads) - 10} more")
        _print(f"If Codex cannot open them (\"Model provider `{source}` not found\"), this moves them under `openai`,\n"
               "the list Codex's official sign-in and the bridge share (quit Codex first):")
        _print(f"    excel-codex threads migrate --from {source}")
        return 0
    if result.threads:
        _print(f"Moved {len(result.threads)} conversation(s) from `{source}` to `openai`: they carry on with Codex's")
        _print("  official sign-in, or through the bridge while it is on.")
        _print(f"  Conversations started with `{source}` from now on are filed under it again.")
        if _had_1m(result):
            _print(_1M_MOVED)
        _print(f"  Codex's conversation index was copied to {result.backup} first.")
    if result.left:
        _print(f"Could not move {len(result.left)} conversation(s): their file is missing or not laid out as expected.")
        for thread in result.left[:10]:
            _print(_thread_line(thread))
    if not result.threads:
        return 1
    _print(_REOPEN_FOR_LIST)
    _print("  `excel-codex threads undo` puts them back.")
    return 0


def cmd_threads(args) -> int:
    from . import codex_threads

    home = desktop_config.codex_home()
    record_dir = codex_config.state_dir()
    source = getattr(args, "source", None)
    if source and source != codex_config.PROVIDER_ID and args.action != "undo":
        return _threads_from(args.action, source, home, record_dir)
    try:
        threads = [] if args.action == "undo" else codex_threads.bridge_threads(home, record_dir)
        unfinished = [] if args.action == "undo" else codex_threads.unfinished_threads(home, record_dir)
        if args.action == "migrate" and (threads or unfinished):
            # Not signed in: `migrate` refuses first, saying why.
            if codex_config.codex_signed_in(home) and codex_threads.codex_running():
                _print(_QUIT_CODEX)
                return 1
            result = codex_threads.migrate(home, record_dir)
        elif args.action == "undo":
            if (record_dir / codex_threads.RECORD_NAME).exists() and codex_threads.codex_running():
                _print(_QUIT_CODEX)
                return 1
            undone = codex_threads.undo(home, record_dir)
    except (codex_threads.Refused, OSError, sqlite3.Error) as exc:
        _print(f"Nothing was changed: {exc}")
        return 1

    if args.action == "migrate":
        if not threads and not unfinished:
            _print("No conversations are filed under the bridge's own provider; nothing to move.")
            return 0
        if result.threads:
            _print(f"Moved {len(result.threads)} conversation(s) into the list shared with Codex's official sign-in;")
            _print("  they open with the bridge off too.")
        if result.finished:
            _print(f"Finished {len(result.finished)} conversation(s) moved by an earlier excel-codex: with the bridge off")
            _print("  they carry on with OpenAI's models now.")
        if _had_1m(result):
            _print(_1M_MOVED)
        if result.threads or result.finished:
            _print(f"  Codex's conversation index was copied to {result.backup} first.")
        if result.left:
            _print(f"Could not move {len(result.left)} conversation(s): their file is missing or not laid out as expected.")
            for thread in result.left[:10]:
                _print(_thread_line(thread))
        if not result.threads and not result.finished:
            return 1
        _print(_REOPEN_FOR_LIST)
        _print("  `excel-codex threads undo` puts them back.")
        return 0
    if args.action == "undo":
        if not undone.restored and not undone.kept:
            _print("Nothing to undo: `excel-codex threads migrate` has not moved any conversations.")
            return 0
        _print(f"Put back {undone.restored} conversation(s) under the provider they were filed under before.")
        if undone.kept:
            _print(f"  {undone.kept} changed since (continued with another model, or deleted) and were left as they are.")
        _print(_REOPEN_FOR_LIST)
        return 0

    shared = codex_config.codex_signed_in(home)
    if unfinished:
        _print(f"{len(unfinished)} conversation(s) moved by an earlier excel-codex still name the bridge's models in their\n"
               "file, so with the bridge off Codex may go back to those and fail. `excel-codex threads migrate`\n"
               "finishes them (Codex quit), and so do `excel-codex desktop` and `excel-codex` when they start.")
    if not threads:
        _print("No conversations are filed under the bridge's own provider.")
        if shared:
            _print("  Conversations with and without the bridge are all in one list.")
        _other_providers(home)
        return 0
    _print(f"{len(threads)} conversation(s) are filed under the bridge's own provider "
           "(from excel-codex 0.5.3 and earlier, or while Codex was not signed in):")
    for thread in threads[:10]:
        _print(_thread_line(thread))
    if len(threads) > 10:
        _print(f"    … and {len(threads) - 10} more")
    if shared:
        _print("With the bridge off, Codex cannot open them (\"Model provider `excel-bridge` not found\").\n"
               "`excel-codex desktop` and `excel-codex` move them into the shared list by themselves when\n"
               "started while Codex is fully quit; `excel-codex threads migrate` does it now (Codex quit too).")
    else:
        _print("Codex is not signed in, so the bridge is still their provider and lists them while it is on.\n"
               "After `codex login`, they move into the list shared with it.")
    _other_providers(home)
    return 0


# ─── entry ────────────────────────────────────────────────────────────────────

def _parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--route", choices=upstream_routes.CHOICES, default=upstream_routes.default(),
                        help="upstream: codex (default, native HTTP) or excel (legacy BPS); never silently switches routes")
    common.add_argument(
        "--proxy",
        help="upstream proxy, e.g. http://127.0.0.1:7890 "
        "(default: HTTPS_PROXY / system proxy)",
    )
    common.add_argument(
        "--webview-dir",
        help="Excel WebView2 root (the ...\\Microsoft\\Office folder); for WSL or custom installs",
    )
    common.add_argument(
        "--login",
        choices=session.LOGINS,
        help="ChatGPT sign-in: the native route requires codex; the Excel route accepts auto, codex or excel "
        f"(Excel route default: ${session.LOGIN_ENV} or auto)",
    )
    common.add_argument(
        "--timezone",
        choices=exit_timezone.MODES,
        help="Excel route only: the timezone and date Codex's requests carry: auto (the proxy exit's, "
        f"looked up from its IP; the default) or off (this computer's) (default: ${exit_timezone.MODE_ENV} or auto)",
    )

    auto = argparse.ArgumentParser(add_help=False)
    auto.add_argument(
        "--no-auto-signin",
        action="store_true",
        help="Excel route only: never open Excel to sign in or refresh the session (Windows)",
    )

    drawing = argparse.ArgumentParser(add_help=False)
    drawing.add_argument(
        "--image-model",
        type=_image_model,
        help=f"Excel route only: the image model requested by Codex's image tool (default: ${image_generation.MODEL_ENV} "
        f"or {image_generation.MODEL}, the add-in's)",
    )

    parser = argparse.ArgumentParser(
        prog="excel-codex",
        description="Run Codex locally through an explicit native Codex or legacy Excel route using your own ChatGPT sign-in.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    codex = sub.add_parser(
        "codex",
        parents=[common, auto, drawing],
        help="start the bridge and Codex together (default); extra args go to Codex",
    )
    codex.add_argument("--model", default=codex_config.DEFAULT_MODEL, help="model from the selected route's catalog")
    codex.add_argument("--port", type=int, default=0, help="bridge port (default: a free port)")
    codex.add_argument("--codex", help="path to the Codex executable")
    codex.add_argument(
        "--skip-session-check", action="store_true", help="start even if no session is found yet"
    )

    desktop = sub.add_parser(
        "desktop",
        parents=[common, auto, drawing],
        help="route the Codex desktop app / IDE extension through the bridge while this runs",
    )
    desktop.add_argument("--model", default=codex_config.DEFAULT_MODEL, help="model from the selected route's catalog")
    desktop.add_argument("--port", type=int, default=codex_config.DEFAULT_PORT)
    desktop.add_argument("--off", action="store_true", help="only put the Codex config back, then exit")
    desktop.add_argument(
        "--keep-config", action="store_true", help="leave the config in place when this window closes"
    )
    desktop.add_argument(
        "--keep-apps", action="store_true",
        help="leave Codex's apps and plugin suggestions on (they wait on chatgpt.com)",
    )
    desktop.add_argument(
        "--skip-session-check", action="store_true", help="start even if no session is found yet"
    )

    serve = sub.add_parser(
        "serve", parents=[common, auto, drawing], help="run only the bridge (for IDE / desktop Codex)"
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=codex_config.DEFAULT_PORT)
    serve.add_argument("--log-file", help=argparse.SUPPRESS)
    serve.add_argument("--exit-with-stdin", action="store_true", help=argparse.SUPPRESS)

    sub.add_parser(
        "restore",
        help="put Codex back on its own setup: the bridge's settings out of config.toml (also ones pasted in "
        "by hand), its own conversations into Codex's list, the Windows timezone back",
    )
    sub.add_parser("status", parents=[common], help="show which ChatGPT sign-in the bridge would use")
    check = sub.add_parser("check-route", parents=[common],
                           help="verify native Codex tool calling and continuation (at most 2 inference requests)")
    check.add_argument("--model", default=codex_config.DEFAULT_MODEL)
    check.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max"), default="low")

    login = sub.add_parser(
        "login", parents=[common, auto], help="sign in with Codex, or open Excel's ChatGPT pane with --route excel"
    )
    login.add_argument("--force", action="store_true", help="open the pane even if the session is fine")

    config = sub.add_parser("print-config", parents=[common], help="print a config.toml snippet for `serve` mode")
    config.add_argument("--port", type=int, default=codex_config.DEFAULT_PORT)
    config.add_argument("--model", default=codex_config.DEFAULT_MODEL)
    timezone = sub.add_parser("timezone", help="show the proxy exit's timezone and how Codex is kept on it")
    actions = timezone.add_subparsers(dest="action")
    sync = actions.add_parser("sync", help="Windows: set the system timezone to Codex's exit once now")
    sync.add_argument("--probe", action="store_true", help="only show what it would set")
    actions.add_parser("restore", help="Windows: put back the timezone from before excel-codex first changed it")
    threads = sub.add_parser(
        "threads", help="show conversations filed under the bridge's own provider, which need the bridge on"
    )
    from_help = ("another provider whose conversations Codex cannot open (\"Model provider `<name>` not found\"), "
                 "such as `OpenAI`, as a relay's config template may name it; case-sensitive")
    threads.add_argument("--from", dest="source", metavar="PROVIDER", help=from_help)
    moves = threads.add_subparsers(dest="action")
    migrate = moves.add_parser(
        "migrate",
        help="move them into the list shared with Codex's official sign-in now; with Codex quit "
        "(`desktop` does it by itself)",
    )
    migrate.add_argument("--from", dest="source", metavar="PROVIDER", default=argparse.SUPPRESS, help=from_help)
    moves.add_parser("undo", help="put back what `threads migrate` moved")
    update = sub.add_parser(
        "update",
        help="download the newest release; the Windows package installs it the next time "
        "excel-codex-desktop.cmd starts, which checks by itself",
    )
    update.add_argument("--launcher", action="store_true", help=argparse.SUPPRESS)
    sub.add_parser("sub2api", add_help=False, help="opt-in SUB2API sidecar and SSH session sync")
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
    try:
        return _entry(argv)
    except ValueError as exc:
        _print(str(exc))
        return 2


def _entry(argv: list[str] | None = None) -> int:
    if argv is None and _started_by_double_click():
        # Explorer starts us in the install folder; keep Codex out of it, and
        # keep the window open long enough to read an error or an update notice.
        if Path.cwd().resolve() == Path(sys.executable).resolve().parent:
            os.chdir(Path.home())
        code = _main(sys.argv[1:])
        if code or _update_shown.is_set():
            try:
                input("\nPress Enter to close this window...")
            except (EOFError, KeyboardInterrupt):
                pass
        return code
    return _main(list(sys.argv[1:] if argv is None else argv))


def _main(argv: list[str]) -> int:
    argv = list(argv)
    if argv and argv[0] == "sub2api":
        from .sub2api_cli import main as sub2api_main
        return sub2api_main(argv[1:])
    known = {"codex", "desktop", "restore", "serve", "status", "check-route", "login", "print-config", "timezone", "threads",
             "update", "-h", "--help", "--version"}
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
    if args.command == "check-route":
        return cmd_check_route(args)
    if args.command == "login":
        return cmd_login(args)
    if args.command == "desktop":
        return cmd_desktop(args)
    if args.command == "restore":
        return cmd_restore(args)
    if args.command == "print-config":
        return cmd_print_config(args)
    if args.command == "timezone":
        return cmd_timezone(args)
    if args.command == "threads":
        return cmd_threads(args)
    if args.command == "update":
        return cmd_update(args)
    return cmd_codex(args, codex_args)

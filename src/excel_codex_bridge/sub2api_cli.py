"""Explicit opt-in CLI for the SUB2API sidecar (no automatic credential export)."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import time
from pathlib import Path

import httpx
import uvicorn

from .session import SessionReader
from .sub2api import GatewayKeys, MAX_SESSION_BYTES, SESSION_PATH, create_app, read_secret


def _port(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return number


def _ssh_target(value: str) -> str:
    if not re.fullmatch(r"(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise argparse.ArgumentTypeError("use an SSH host alias or user@hostname (no shell syntax)")
    return value


def _container(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise argparse.ArgumentTypeError("invalid Docker container name")
    return value


def _watch_interval(value: str) -> int:
    interval = int(value)
    if not 30 <= interval <= 86400:
        raise argparse.ArgumentTypeError("watch interval must be 30-86400 seconds")
    return interval


def _parser():
    parser = argparse.ArgumentParser(
        prog="excel-sub2api",
        description="Opt-in Excel upstream for SUB2API. Session transfer requires explicit SSH sync.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="serve on a private network with separate API/admin keys")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=_port, default=8000)
    init = sub.add_parser("init-secrets", help="create two random secret files; never overwrite")
    init.add_argument("directory", type=Path)
    push = sub.add_parser("push-session", help="send your local Excel session to your server over SSH")
    push.add_argument("--ssh", required=True, type=_ssh_target, help="trusted SSH host alias or user@host")
    push.add_argument("--ssh-port", type=_port)
    push.add_argument("--identity-file", type=Path)
    push.add_argument("--container", type=_container, default="excel-sub2api")
    push.add_argument("--port", type=_port, default=8000, help="port inside the sidecar container")
    push.add_argument("--sudo", action="store_true", help="use sudo -n for remote Docker")
    push.add_argument("--webview-dir", type=Path)
    push.add_argument("--watch", type=_watch_interval, metavar="SECONDS",
                      help="resync until stopped; also restores sessions after container restarts")
    for command in ("import-session", "session-status", "clear-session"):
        local = sub.add_parser(command, help="control plane: run inside the sidecar container")
        local.add_argument("--port", type=_port, default=8000)
    return parser


def init_secrets(directory: Path) -> None:
    paths = [directory / name for name in ("api-key", "admin-key")]
    if any(path.exists() or path.is_symlink() for path in paths):
        raise ValueError("Secret files already exist; nothing was overwritten")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    for path in paths:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(secrets.token_urlsafe(48) + "\n")


def _safe_summary(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise RuntimeError("The sidecar returned an invalid session summary")
    result = {}
    for key in ("configured", "expired"):
        if not isinstance(payload.get(key), bool):
            raise RuntimeError("The sidecar returned an invalid session summary")
        result[key] = payload[key]
    for key in ("expires_at", "expires_in_seconds"):
        value = payload.get(key)
        if value is not None and type(value) not in (int, float):
            raise RuntimeError("The sidecar returned an invalid session summary")
        result[key] = value
    return result


def control_request(command: str, port: int, raw: bytes | None = None) -> dict:
    admin_key = read_secret("EXCEL_SUB2API_ADMIN_KEY")
    method = {"import-session": "POST", "clear-session": "DELETE", "session-status": "GET"}[command]
    # A proxy must never receive this key or the session. No redirects either.
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=15.0) as client:
        response = client.request(
            method, f"http://127.0.0.1:{port}{SESSION_PATH}",
            headers={"authorization": f"Bearer {admin_key}", "content-type": "application/json"},
            content=raw,
        )
    if response.status_code != 200:
        raise RuntimeError(f"Sidecar control request failed (HTTP {response.status_code})")
    try:
        return _safe_summary(response.json())
    except ValueError:
        raise RuntimeError("The sidecar returned invalid JSON") from None


def session_payload(reader: SessionReader) -> bytes:
    status = reader.refresh(force=True)
    if reader.last_error or not status.get("configured") or status.get("expired"):
        raise RuntimeError("No usable local Excel session; sign in with excel-codex login first")
    headers = reader.store.request_headers(stream=False)
    payload = json.dumps({"headers": headers, "tools_version_id": reader.store.tools_version_id()}).encode()
    if len(payload) > MAX_SESSION_BYTES:
        raise RuntimeError("Excel session payload is too large")
    return payload


def push_session(args, reader: SessionReader) -> dict:
    payload = session_payload(reader)
    remote = (["sudo", "-n"] if args.sudo else []) + [
        "docker", "exec", "-i", args.container, "excel-sub2api",
        "import-session", "--port", str(args.port),
    ]
    command = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    if args.ssh_port:
        command += ["-p", str(args.ssh_port)]
    if args.identity_file:
        command += ["-i", str(args.identity_file)]
    command += [args.ssh, shlex.join(remote)]
    # SSH authenticates/encrypts the channel. The token is stdin, never argv,
    # a temporary file, environment, or a printed diagnostic. Host keys remain
    # verified by the user's normal SSH configuration.
    result = subprocess.run(command, input=payload, capture_output=True, timeout=60, check=False)
    if result.returncode:
        raise RuntimeError(
            f"SSH session import failed (exit {result.returncode}); check SSH trust, Docker access and sidecar status"
        )
    try:
        summary = _safe_summary(json.loads(result.stdout))
    except (ValueError, UnicodeError):
        raise RuntimeError("SSH session import returned an invalid summary") from None
    if not summary["configured"] or summary["expired"]:
        raise RuntimeError("The sidecar did not accept a usable session")
    return summary


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "serve":
            app = create_app(GatewayKeys.from_env())
            uvicorn.run(app, host=args.host, port=args.port, proxy_headers=False,
                        access_log=False, log_level="warning", limit_concurrency=32)
        elif args.command == "init-secrets":
            init_secrets(args.directory)
            print("Created api-key and admin-key; keep them private and out of Git.")
        elif args.command == "push-session":
            # Only the Excel add-in's session goes to the sidecar, as before; Codex's
            # own sign-in stays on this computer.
            reader = SessionReader(webview_root=args.webview_dir, login="excel")
            while True:
                try:
                    print(json.dumps(push_session(args, reader)), flush=True)
                except (RuntimeError, OSError, subprocess.SubprocessError):
                    if not args.watch:
                        raise
                    print("Session sync failed; check Excel sign-in and SSH/sidecar access. Retrying.",
                          file=sys.stderr, flush=True)
                if not args.watch:
                    break
                time.sleep(args.watch)
        else:
            raw = None
            if args.command == "import-session":
                raw = sys.stdin.buffer.read(MAX_SESSION_BYTES + 1)
                if len(raw) > MAX_SESSION_BYTES:
                    raise ValueError("Session payload is too large")
            print(json.dumps(control_request(args.command, args.port, raw)))
    except KeyboardInterrupt:
        return 130
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, httpx.HTTPError, subprocess.SubprocessError) as exc:
        # Exception repr/str can include a request's body, URL or subprocess
        # input/output. Print only its type, including for TimeoutExpired.
        print(f"SUB2API operation failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

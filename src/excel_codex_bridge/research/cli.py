"""The default command lists candidates offline and never starts inference."""

import argparse
import asyncio
import json
from pathlib import Path
import secrets

from .. import __version__, codex_config
from . import config, runner, siwc, tasks
from .credentials import siwc_path
from .report import write_report, report
from .storage import digest
from .transport import wire_payload


def parser():
    root = argparse.ArgumentParser(prog="codex-channels", description="Explicit multi-channel research; no inference until run.")
    root.add_argument("--version", action="version", version=__version__)
    sub = root.add_subparsers(dest="command")
    sub.add_parser("routes", help="list implemented adapters, offline")
    p = sub.add_parser("prepare", help="freeze tasks, route order, credentials references and a total call cap")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--config", type=Path, help="JSON configuration with references only, no tokens")
    p.add_argument("--routes", default=",".join(config.DEFAULT_ROUTES))
    p.add_argument("--profile", choices=config.CAPS, default="smoke")
    p.add_argument("--model", default=codex_config.DEFAULT_MODEL)
    p.add_argument("--effort", default="low")
    p.add_argument("--seed", type=int)
    p.add_argument("--max-calls", type=int)
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--window-gap", type=int, default=3600)
    for name in ("inventory", "request-diff", "run", "status", "report"):
        p = sub.add_parser(name)
        p.add_argument("--study", type=Path, required=True)
        if name == "run":
            p.add_argument("--window", type=int, default=1)
    p = sub.add_parser("login-siwc", help="explicit browser login; validates a separate SIWC grant")
    p.add_argument("--auth-file", type=Path, default=siwc_path())
    p.add_argument("--no-browser", action="store_true")
    return root


def execute(args):
    if args.command in (None, "routes"):
        return {"version": __version__, "inference_started": False, "routes": [
            {"kind": kind, "bridge_route": {"codex-http": "codex", "codex-ws": "codex-ws", "bps": "excel"}.get(kind),
             "research_tools": kind not in {"codex-cli", "bps"}, "credential": "Codex login" if kind.startswith("codex") or kind == "bps" else "SIWC grant" if kind == "siwc" else "explicit API key reference"}
            for kind in config.KINDS]}
    if args.command == "login-siwc":
        return siwc.login(args.auth_file.expanduser(), browser=not args.no_browser)
    if args.command == "prepare":
        if args.config:
            value = json.loads(args.config.read_text(encoding="utf-8-sig"))
        else:
            value = {"profile": args.profile, "routes": [{"id": kind, "kind": kind} for kind in args.routes.split(",")],
                     "model": args.model, "effort": args.effort, "seed": args.seed if args.seed is not None else secrets.randbits(48),
                     "timeout_seconds": args.timeout, "window_gap_seconds": args.window_gap}
            if args.max_calls is not None:
                value["max_calls"] = args.max_calls
        return config.prepare(value, args.out)
    manifest, ledger = config.open_study(args.study)
    try:
        if args.command == "inventory":
            return {"inference_started": False, "routes": runner.inventory(manifest)}
        if args.command == "request-diff":
            body = tasks.payload(manifest["tasks"][0], manifest["config"]["model"], manifest["config"]["effort"])
            changes = []
            for route in manifest["config"]["routes"]:
                try:
                    wire = wire_payload(route["kind"], body)
                    changes.append({"route": route["id"], "body_hash": digest(wire), "body": wire,
                        "cli_harness_additions": route["kind"] == "codex-cli", "gateway_internal_changes_known": route["kind"] != "responses-http"})
                except ValueError:
                    changes.append({"route": route["id"], "unsupported": True})
            return {"inference_started": False, "common_body_hash": digest(body), "first_task": changes}
        if args.command == "run":
            result = asyncio.run(runner.run(manifest, ledger, window=args.window))
            summary = write_report(manifest, ledger)
            return {**result, "routes": summary["routes"], "report": str(args.study.resolve() / "report.md")}
        result = write_report(manifest, ledger) if args.command == "report" else report(manifest, ledger)
        return {key: value for key, value in result.items() if key not in {"calls", "bindings", "limits"}}
    finally:
        ledger.close()


def main(argv=None):
    try:
        result = execute(parser().parse_args(argv))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        print('{"error":"interrupted; submitted work remains charged and is never replayed"}')
        return 130
    except Exception as exc:
        # Network, SQL, subprocess and credential exception strings can contain secrets.
        print(json.dumps({"error": type(exc).__name__, "action": "Inspect the frozen configuration, credential references and ledger. No automatic retry was attempted."}))
        return 1

"""Execute each frozen arm once, charging before dispatch and grading locally."""

import hashlib
import time

from . import tasks
from .credentials import Unavailable, resolve
from .storage import BudgetExhausted, exclusive
from .transport import Result, Transport


def inventory(manifest, resolver=resolve):
    rows = []
    for route in manifest["config"]["routes"]:
        try:
            snapshot = resolver(route)
            rows.append({**snapshot.identity, "endpoint": snapshot.endpoint, "ready": True})
        except Unavailable as exc:
            rows.append({"route": route["id"], "kind": route["kind"], "ready": False, "reason": str(exc)})
        except (OSError, ValueError):
            rows.append({"route": route["id"], "kind": route["kind"], "ready": False, "reason": "credentials_or_executable_unavailable"})
    return rows


async def run(manifest, ledger, *, window=1, resolver=resolve, transport=None):
    config = manifest["config"]
    if window not in ({1, 2} if config["profile"] == "confirm" else {1}):
        raise ValueError("Window does not belong to this frozen profile")
    transport = transport or Transport()
    route_map = {r["id"]: r for r in config["routes"]}
    task_map = {t["id"]: t for t in manifest["tasks"]}
    stopped = None
    try:
        with exclusive(ledger.directory / "run.lock"):
            ledger.open_window(window)
            outcomes = {r["arm"] for r in ledger.rows("outcomes")}
            calls = ledger.rows("calls")
            # A crashed process lost its in-memory continuation. Never resubmit it.
            for arm in {call["arm"] for call in calls} - outcomes:
                for call in (c for c in calls if c["arm"] == arm and c["result"] is None):
                    ledger.finish(call["id"], Result("completion_unknown_interrupted").record())
                ledger.outcome(arm, {"status": "interrupted_not_replayed", "grade": None, "identity_ok": False})
                outcomes.add(arm)
            disabled = {r["route"] for r in ledger.rows("disabled")}
            checked = set()
            for arm in manifest["schedule"]:
                if arm["window"] != window or arm["arm"] in outcomes:
                    continue
                route, task = route_map[arm["route"]], task_map[arm["task"]]
                skip = "route_stopped" if route["id"] in disabled else None
                if task["tools"] and route["kind"] in {"codex-cli", "bps"}:
                    skip = "unsupported_tools"
                if skip:
                    ledger.outcome(arm["arm"], {"status": skip, "grade": None, "identity_ok": False})
                    continue
                try:
                    snapshot = resolver(route)
                except (Unavailable, OSError, ValueError):
                    ledger.outcome(arm["arm"], {"status": "not_ready", "grade": None, "identity_ok": False})
                    continue
                ledger.bind(route["id"], snapshot.identity)
                if route["id"] not in checked:
                    try:
                        await transport.preflight(route, snapshot, config)
                    except Exception:
                        # Discovery is not inference, but must succeed before submitting.
                        ledger.disable(route["id"], "preflight_failed")
                        disabled.add(route["id"])
                        ledger.outcome(arm["arm"], {"status": "preflight_failed", "grade": None, "identity_ok": True})
                        continue
                    checked.add(route["id"])
                payload, seen, records = tasks.payload(task, config["model"], config["effort"]), set(), []
                result, identity_ok = Result("not_submitted"), True
                for turn in range(task["max_turns"]):
                    current = resolver(route)
                    ledger.bind(route["id"], current.identity)
                    try:
                        call_id = ledger.reserve(arm["arm"], route["id"], turn + 1)
                    except BudgetExhausted:
                        stopped = "budget_exhausted"
                        result = Result(stopped)
                        break
                    try:
                        result = await transport.call(route, current, payload, config["timeout_seconds"])
                    except BaseException:
                        ledger.finish(call_id, Result("completion_unknown_interrupted").record())
                        raise
                    try:
                        identity_ok = resolver(route).identity == current.identity
                    except (Unavailable, OSError, ValueError):
                        identity_ok = False
                    if not identity_ok:
                        result.status = "identity_changed"
                    record = {**result.record(), "identity_ok": identity_ok}
                    ledger.finish(call_id, record)
                    records.append(record)
                    if result.status != "completed":
                        break
                    if not any(item.get("type") == "function_call" for item in result.output):
                        break
                    if turn + 1 >= task["max_turns"]:
                        result.status = "tool_turn_limit"
                        break
                    try:
                        payload["input"].extend(tasks.continue_tool(task, result.output, seen))
                    except (ValueError, TypeError, KeyError):
                        result.status = "invalid_tool_continuation"
                        break
                scored = tasks.grade(task, result.answer, len(seen)) if result.status == "completed" and identity_ok else None
                ledger.outcome(arm["arm"], {"status": result.status, "grade": scored, "identity_ok": identity_ok,
                    "reported_model": result.model, "reported_effort": result.effort,
                    "answer_sha256": hashlib.sha256(result.answer.encode()).hexdigest() if result.answer else None,
                    "calls": len(records), "seconds": round(sum(r["seconds"] for r in records), 3)})
                if result.status != "completed" and result.status != "budget_exhausted":
                    ledger.disable(route["id"], result.status)
                    disabled.add(route["id"])
                if not identity_ok:
                    stopped = "identity_changed"
                if stopped:
                    break
    finally:
        await transport.aclose()
    return {"stopped": stopped, "reserved_submissions": len(ledger.rows("calls")), "max_calls": config["max_calls"], "window": window}

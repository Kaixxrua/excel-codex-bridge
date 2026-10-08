"""A bounded native tool/continuation acceptance check, not a quality benchmark."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import tempfile
import time

from .native_upstream import BASE_URL, NativeBridge, RouteError


def save(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".route-check-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


async def run(reader, client_factory, *, model: str, effort: str, receipt: Path, timeout: float = 90, route: str = "codex") -> dict:
    from .upstream_routes import NATIVE, create_bridge
    if route not in NATIVE:
        raise ValueError("Select a native route")
    bridge = create_bridge(reader, client_factory, route)
    endpoint = BASE_URL + "/responses" if route == "codex" else "wss://chatgpt.com/backend-api/codex/responses"
    report = {"route": route, "endpoint": endpoint, "model": model, "effort": effort,
              "checked_at": time.time(), "max_calls": 2, "calls": [], "status": "not_started",
              "protocol_verified": False, "quality_advantage_proven": False}
    value = secrets.token_hex(8)
    payload = {"model": model, "reasoning": {"effort": effort}, "store": False, "stream": False,
               "instructions": "Follow the user's tool-protocol acceptance check exactly.",
               "include": ["reasoning.encrypted_content"],
               "input": [{"role": "user", "content": "Call bridge_probe.read_value exactly once, then reply with only the value returned by that tool."}],
               "tools": [{"type": "namespace", "name": "bridge_probe", "description": "Local acceptance fixture.",
                          "tools": [{"type": "function", "name": "read_value", "description": "Return the fixture value.",
                                     "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
                                     "strict": True}]}]}

    async def call(stage):
        # Persist the submission reservation before issuing an inference request.
        entry = {"stage": stage, "status": "submitted"}
        report["calls"].append(entry)
        report["status"] = "running"
        save(receipt, report)
        started = time.monotonic()
        response = await asyncio.wait_for(bridge.responses(payload), timeout)
        entry["seconds"] = round(time.monotonic() - started, 3)
        entry["http_status"] = response.status_code
        body = json.loads(response.body)
        if not isinstance(body, dict):
            raise RouteError("The inference response was not a protocol object.")
        entry["status"] = body.get("status", "http_error")
        entry["reported_model"] = body.get("model")
        entry["reported_effort"] = (body.get("reasoning") or {}).get("effort")
        save(receipt, report)
        if response.status_code != 200 or body.get("status") != "completed":
            raise RouteError("The inference request did not complete.")
        if body.get("model") != model or (entry["reported_effort"] is not None and entry["reported_effort"] != effort):
            raise RouteError("The upstream reported a different model or reasoning effort.")
        if bridge.headers()["ChatGPT-Account-Id"] != account:
            raise RouteError("The selected account changed during the check.")
        return body

    try:
        account = bridge.headers()["ChatGPT-Account-Id"]
        report["account_ref"] = hashlib.sha256(account.encode()).hexdigest()[:24]
        save(receipt, report)
        catalog = await bridge.catalog()
        capabilities = next((m for m in catalog["models"] if m["slug"] == model), None)
        if capabilities is None:
            raise RouteError("The selected model is absent from this account's catalog.")
        efforts = {r["effort"] for r in capabilities.get("supported_reasoning_levels", [])}
        if efforts and effort not in efforts:
            raise RouteError("The selected effort is absent from this model's catalog.")
        first = await call("tool_call")
        if not isinstance(first.get("output"), list) or any(not isinstance(item, dict) for item in first["output"]):
            raise RouteError("The inference response contained invalid output items.")
        calls = [item for item in first.get("output", []) if item.get("type") == "function_call"]
        if len(calls) != 1 or calls[0].get("name") not in {"read_value", "bridge_probe.read_value"} or not calls[0].get("call_id"):
            raise RouteError("The model did not produce the required fixture tool call.")
        if calls[0].get("namespace") not in (None, "bridge_probe") or json.loads(calls[0]["arguments"]) != {}:
            raise RouteError("The fixture tool arguments did not match the contract.")
        payload["input"].extend(first["output"])
        payload["input"].append({"type": "function_call_output", "call_id": calls[0]["call_id"],
                                 "output": json.dumps({"value": value})})
        second = await call("continuation")
        answer = "".join(part.get("text", "") for item in second.get("output", [])
                         if item.get("type") == "message" for part in item.get("content", [])
                         if part.get("type") == "output_text")
        if answer.strip() != value:
            raise RouteError("The continuation did not return the fixture value.")
        report["status"], report["protocol_verified"] = "passed", True
    except RouteError as exc:
        report["status"], report["error"] = "failed", str(exc)
    except (TimeoutError, asyncio.TimeoutError):
        report["status"], report["error"] = "failed", "timeout; upstream completion is unknown"
    except (ValueError, KeyError, TypeError, AttributeError):
        report["status"], report["error"] = "failed", "invalid protocol response"
    finally:
        if report["status"] in {"not_started", "running"}:
            report["status"] = "interrupted"
        for entry in report["calls"]:
            if entry["status"] == "submitted":
                entry["status"] = "completion_unknown"
        save(receipt, report)
        await bridge.aclose()
    return report

from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

import httpx
import pytest

from excel_codex_bridge import sse
from excel_codex_bridge.research import config, runner, tasks
from excel_codex_bridge.research.credentials import Snapshot, resolve, Unavailable
from excel_codex_bridge.research.report import report, write_report
from excel_codex_bridge.research.storage import BudgetExhausted, Ledger, exclusive, private_directory
from excel_codex_bridge.research.transport import Result, Transport, wire_payload
from helpers import write_codex_login

MODEL = "gpt-5.6-sol"


def settings(**extra):
    return {"model": MODEL, "seed": 77, "max_calls": 6, "routes": [
        {"id": "http", "kind": "codex-http"}, {"id": "ws", "kind": "codex-ws"}], **extra}


def snapshot(route, account="same-account"):
    return Snapshot({"route": route["id"], "kind": route["kind"], "account_ref": account,
                     "identity_basis": "selected_codex_account", "credential_reference": route["id"]},
                    "https://chatgpt.com/backend-api/codex/responses", {"Authorization": "Bearer secret-token"})


class OracleTransport:
    def __init__(self, manifest, *, fail=False):
        self.tasks = manifest["tasks"]
        self.calls = []
        self.fail = fail
    async def aclose(self):
        pass
    async def preflight(self, *args):
        pass
    async def call(self, route, selected, body, timeout):
        self.calls.append(copy.deepcopy(body))
        if self.fail:
            return Result("http_error", http_status=403)
        task = next(t for t in self.tasks if t["prompt"] == body["input"][0]["content"])
        if task["tools"] and len(body["input"]) == 1:
            return Result("completed", output=[{"type": "reasoning", "encrypted_content": "sealed-never-log"},
                {"type": "function_call", "name": "fixture_read", "call_id": "fixture-1", "arguments": json.dumps({"key": task["fixture"]["key"]})}],
                model=MODEL, effort="low", usage={"input_tokens": 10, "output_tokens": 5})
        return Result("completed", answer=json.dumps(task["expected"]), model=MODEL, effort="low", seconds=0.1)


def study(tmp_path, **kwargs):
    directory = tmp_path / "study"
    config.prepare(settings(**kwargs), directory)
    return config.open_study(directory)


def test_freeze_detects_mutation_and_never_recreates_ledger(tmp_path):
    manifest, ledger = study(tmp_path)
    ledger.close()
    altered = copy.deepcopy(manifest)
    altered["config"]["max_calls"] = 9
    with pytest.raises(ValueError, match="changed"):
        Ledger(tmp_path / "study", altered)
    (tmp_path / "study" / "ledger.sqlite3").unlink()
    with pytest.raises(ValueError, match="missing"):
        config.open_study(tmp_path / "study")


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory mode check")
def test_existing_shared_directory_permissions_are_not_changed(tmp_path):
    directory = tmp_path / "existing"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    with pytest.raises(ValueError, match="will not be changed"):
        private_directory(directory)
    assert directory.stat().st_mode & 0o777 == 0o755


def test_budget_and_lock_survive_reopen(tmp_path):
    manifest, ledger = study(tmp_path, max_calls=1)
    call = ledger.reserve("arm", "http", 1)
    ledger.close()
    _, ledger = config.open_study(tmp_path / "study")
    try:
        assert ledger.rows("calls")[0]["result"] is None
        with pytest.raises(BudgetExhausted):
            ledger.reserve("new", "http", 1)
        with exclusive(tmp_path / "study" / "run.lock"):
            with pytest.raises(ValueError, match="already"):
                with exclusive(tmp_path / "study" / "run.lock"):
                    pass
        with exclusive(tmp_path / "study" / "run.lock"):
            pass
    finally:
        ledger.close()


def test_complete_pair_and_resume_do_not_replay(tmp_path):
    manifest, ledger = study(tmp_path)
    transport = OracleTransport(manifest)
    try:
        asyncio.run(runner.run(manifest, ledger, resolver=snapshot, transport=transport))
        result = write_report(manifest, ledger)
        assert len(transport.calls) == 6
        assert all(row["passed_tasks"] == 2 for row in result["routes"])
        assert result["pairs"][0]["same_account_native_comparison"]
        assert result["pairs"][0]["ties"] == 2
        assert not result["quality_advantage_proven"]
        continuation = next(body for body in transport.calls if len(body["input"]) > 1)
        assert continuation["input"][1]["encrypted_content"] == "sealed-never-log"
        raw = (tmp_path / "study" / "report.json").read_text()
        assert "sealed-never-log" not in raw and "secret-token" not in raw
        assert not any(t.get("fixture", {}).get("value", "not-a-fixture") in raw for t in manifest["tasks"])
        resumed = OracleTransport(manifest)
        asyncio.run(runner.run(manifest, ledger, resolver=snapshot, transport=resumed))
        assert resumed.calls == []
    finally:
        ledger.close()


def test_reserved_but_interrupted_arm_is_not_replayed(tmp_path):
    manifest, ledger = study(tmp_path)
    first = manifest["schedule"][0]
    ledger.reserve(first["arm"], first["route"], 1)
    try:
        transport = OracleTransport(manifest)
        asyncio.run(runner.run(manifest, ledger, resolver=snapshot, transport=transport))
        assert ledger.rows("calls")[0]["result"]["status"] == "completion_unknown_interrupted"
        result = next(row for row in ledger.rows("outcomes") if row["arm"] == first["arm"])
        assert result["result"]["status"] == "interrupted_not_replayed"
        assert len(ledger.rows("calls")) <= 6
    finally:
        ledger.close()


def test_failure_stops_each_route_without_retries(tmp_path):
    manifest, ledger = study(tmp_path)
    try:
        transport = OracleTransport(manifest, fail=True)
        asyncio.run(runner.run(manifest, ledger, resolver=snapshot, transport=transport))
        assert len(transport.calls) == 2
        assert len(ledger.rows("disabled")) == 2
        assert all(row["scored_tasks"] == 0 for row in report(manifest, ledger)["routes"])
        assert not report(manifest, ledger)["ceiling_effect"]
    finally:
        ledger.close()


def test_identity_switch_is_detected_before_submission(tmp_path):
    manifest, ledger = study(tmp_path)
    route = manifest["config"]["routes"][0]
    ledger.bind(route["id"], snapshot(route).identity)
    try:
        with pytest.raises(ValueError, match="changed"):
            asyncio.run(runner.run(manifest, ledger, resolver=lambda r: snapshot(r, "different"), transport=OracleTransport(manifest)))
        assert all(c["route"] != route["id"] for c in ledger.rows("calls"))
    finally:
        ledger.close()


def test_confirmation_window_requires_frozen_time_gap(tmp_path):
    manifest, ledger = study(tmp_path, profile="confirm", max_calls=120, window_gap_seconds=60)
    try:
        ledger.open_window(1, now=100)
        with pytest.raises(ValueError, match="previous"):
            ledger.open_window(2, now=160)
        for arm in manifest["schedule"]:
            if arm["window"] == 1:
                ledger.outcome(arm["arm"], {"status":"fixture"})
        with pytest.raises(ValueError, match="not due"):
            ledger.open_window(2, now=159)
        ledger.open_window(2, now=160)
    finally:
        ledger.close()


@pytest.mark.parametrize("extra", [{"token":"secret"}, {"max_calls": True}, {"max_calls": 1000}, {"effort":"ultra"},
    {"routes":[{"id":"x","kind":"responses-http","base_url":"https://secret@host/v1","key_env":"KEY"}]},
    {"routes":[{"id":"x","kind":"responses-http","base_url":"http://example.com/v1","key_env":"KEY"}]},
    {"routes":[{"id":"x","kind":"codex-http","base_url":"https://evil.invalid"}]}])
def test_unsafe_or_inconsistent_configuration_is_rejected(extra):
    with pytest.raises(ValueError):
        config.validate(settings(**extra))


def test_sql_oracle_is_read_only_and_bounded():
    task = next(t for t in tasks.generate("screen", 17) if t["family"] == "sql")
    assert tasks.grade(task, json.dumps({"sql":"SELECT region, SUM(amount) FROM sales WHERE paid=1 GROUP BY region ORDER BY region"}), 0)["passed"]
    for sql in ("DROP TABLE sales", "SELECT load_extension('/tmp/x')", "SELECT * FROM sqlite_master", "ATTACH DATABASE '/tmp/x' AS x"):
        assert not tasks.grade(task, json.dumps({"sql": sql}), 0)["passed"]


def test_tool_contract_rejects_arbitrary_execution_and_duplicate_calls():
    task = next(t for t in tasks.generate("smoke", 3) if t["tools"])
    call = {"type":"function_call", "name":"fixture_read", "call_id":"call", "arguments":json.dumps({"key":task["fixture"]["key"]})}
    seen = set()
    assert tasks.continue_tool(task, [call], seen)[-1]["type"] == "function_call_output"
    with pytest.raises(ValueError):
        tasks.continue_tool(task, [call], seen)
    with pytest.raises(ValueError):
        tasks.continue_tool(task, [{**call, "name":"shell"}], set())


def test_credentials_are_bound_and_cli_cannot_refresh_original(tmp_path):
    auth = write_codex_login(tmp_path / "auth.json", time.time() + 3600)
    original = auth.read_bytes()
    selected = resolve({"id":"http", "kind":"codex-http", "auth_file":str(auth)})
    assert "codex-account" not in json.dumps(selected.identity)
    assert "Bearer" not in repr(selected)
    with patch("excel_codex_bridge.cli._find_codex", return_value="/fake/codex"):
        cli = resolve({"id":"cli", "kind":"codex-cli", "auth_file":str(auth)})
    assert cli.auth["tokens"]["refresh_token"] == ""
    assert auth.read_bytes() == original


def test_bps_adaptation_keeps_requested_model_and_effort():
    body = tasks.payload(tasks.generate("smoke",1)[0],MODEL,"low")
    wire = wire_payload("bps",body)
    assert wire["model"] == MODEL and wire["reasoning_effort"] == "low"
    assert "model_selection" in wire


def test_sse_terminal_errors_and_rejections_are_not_retried():
    async def check():
        body = tasks.payload(tasks.generate("smoke",1)[0],MODEL,"low")
        route = {"id":"http", "kind":"codex-http"}
        completed = {"type":"response.completed", "response":{"object":"response", "status":"completed", "model":MODEL,
            "reasoning":{"effort":"low"}, "output":[{"type":"message","content":[{"type":"output_text","text":"answer"}]}]}}
        cases = [(403,b"", "http_error"), (302,b"", "http_error"), (200,b"data: [DONE]\n\n", "missing_terminal"),
                 (200,sse.sse_encode("response.completed", {"type":"response.completed","response":{"status":"in_progress"}}),"invalid_terminal"),
                 (200,sse.sse_encode("response.completed",completed),"completed")]
        for status, raw, expected in cases:
            calls=[]
            def handler(request):
                calls.append(request)
                return httpx.Response(status, content=raw)
            transport=Transport(lambda:httpx.AsyncClient(transport=httpx.MockTransport(handler)))
            try:
                result=await transport.call(route,snapshot(route),body,1)
                assert result.status==expected
                assert len(calls)==1
            finally:
                await transport.aclose()
    asyncio.run(check())


def test_timeout_remains_unknown_completion():
    async def check():
        async def handler(request):
            await asyncio.Event().wait()
        transport=Transport(lambda:httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        try:
            route={"id":"http","kind":"codex-http"}
            result=await transport.call(route,snapshot(route),tasks.payload(tasks.generate("smoke",1)[0],MODEL,"low"),0.01)
            assert result.status=="completion_unknown_timeout"
        finally:
            await transport.aclose()
    asyncio.run(check())

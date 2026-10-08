"""Offline paired reports. Protocol availability is separate from oracle accuracy."""

import itertools
import statistics

from .storage import write_json


def report(manifest, ledger) -> dict:
    config = manifest["config"]
    outcomes = {r["arm"]: r["result"] for r in ledger.rows("outcomes")}
    bindings = {r["route"]: r["identity"] for r in ledger.rows("bindings")}
    calls = ledger.rows("calls")
    summaries = []
    for route in config["routes"]:
        arms = [a for a in manifest["schedule"] if a["route"] == route["id"]]
        rows = [outcomes[a["arm"]] for a in arms if a["arm"] in outcomes]
        scored = [r for r in rows if r.get("grade") is not None]
        submissions = [c for c in calls if c["route"] == route["id"]]
        completed_calls = [c for c in submissions if c["result"] and c["result"]["status"] == "completed"]
        statuses = {}
        for row in rows:
            statuses[row["status"]] = statuses.get(row["status"], 0) + 1
        summaries.append({"route": route["id"], "kind": route["kind"], "submissions": len(submissions),
            "completed_submissions": len(completed_calls), "scored_tasks": len(scored), "passed_tasks": sum(r["grade"]["passed"] for r in scored),
            "accuracy": sum(r["grade"]["passed"] for r in scored) / len(scored) if scored else None,
            "median_seconds": statistics.median(r["seconds"] for r in scored) if scored else None,
            "pending_arms": len(arms) - len(rows), "statuses": statuses,
            "upstream_submission_count_known": route["kind"] not in {"codex-cli", "responses-http"}})
    pairs = []
    arm_map = {(a["window"], a["task"], a["route"]): outcomes.get(a["arm"]) for a in manifest["schedule"]}
    for left, right in itertools.combinations(config["routes"], 2):
        a, b = bindings.get(left["id"], {}), bindings.get(right["id"], {})
        native = {"codex-http", "codex-ws"}
        comparable = (left["kind"] in native and right["kind"] in native and a.get("account_ref") is not None
                      and a.get("account_ref") == b.get("account_ref"))
        wins, losses, ties, missing = 0, 0, 0, 0
        for window, task in sorted({(a["window"], a["task"]) for a in manifest["schedule"]}):
            rows = [arm_map.get((window, task, route["id"])) for route in (left, right)]
            if any(not row or row.get("grade") is None or not row.get("identity_ok") for row in rows):
                missing += 1
                continue
            if comparable and any(row.get("reported_model") != config["model"] or row.get("reported_effort") != config["effort"] for row in rows):
                missing += 1
                continue
            x, y = (row["grade"]["score"] for row in rows)
            wins += x > y
            losses += x < y
            ties += x == y
        pairs.append({"left": left["id"], "right": right["id"], "same_account_native_comparison": comparable,
                      "left_wins": wins, "right_wins": losses, "ties": ties, "unpaired": missing,
                      "interpretation": "matched_native_transports" if comparable else "exploratory_different_or_unverified_harness"})
    return {"tool_version": manifest.get("tool_version", "unrecorded"), "runtime": manifest.get("runtime", {}),
        "profile": config["profile"], "model": config["model"], "effort": config["effort"],
        "reserved_submissions": len(calls), "max_calls": config["max_calls"],
        "unknown_completions": sum(c["result"] is None or c["result"]["status"].startswith("completion_unknown") for c in calls),
        "routes": summaries, "pairs": pairs, "calls": calls, "bindings": bindings, "disabled": ledger.rows("disabled"),
        "windows": ledger.rows("windows"), "quality_advantage_proven": False,
        "ceiling_effect": any(r.get("grade") is not None for r in outcomes.values()) and all(r["grade"]["passed"] for r in outcomes.values() if r.get("grade") is not None),
        "limits": ["Synthetic fixture accuracy does not establish general model quality or capability restoration.",
                   "Correlated task variants and repeated windows are not independent samples; paired wins are descriptive.",
                   "CLI prompts include its own harness. CLI/gateway internal retries and billing are not attested by this ledger.",
                   "Timeouts bound client waiting, not remote generation or token charges. No request is silently replayed.",
                   "SIWC subjects are not workspace IDs; generic API keys do not establish the upstream account.",
                   "BPS research is text-only and includes its protocol adaptation; legacy production retries are excluded."]}


def write_report(manifest, ledger):
    result = report(manifest, ledger)
    write_json(ledger.directory / "report.json", result)
    lines = ["# Codex Channels 对照报告", "", f"模型：{result['model']}；推理档位：{result['effort']}；阶段：{result['profile']}",
        f"已预留提交：{result['reserved_submissions']} / {result['max_calls']}；完成状态未知：{result['unknown_completions']}", "",
        "| 通道 | 提交 / 完成 | 已评分 / 通过 | 完成任务耗时中位数 | 尚未执行 |", "|---|---:|---:|---:|---:|"]
    for row in result["routes"]:
        seconds = "—" if row["median_seconds"] is None else f"{row['median_seconds']:.3f}s"
        lines.append(f"| {row['route']} | {row['submissions']} / {row['completed_submissions']} | {row['scored_tasks']} / {row['passed_tasks']} | {seconds} | {row['pending_arms']} |")
    lines += ["", "## 配对结果", "", "| 左 / 右 | 左胜 / 右胜 / 平 | 无法配对 | 身份与执行条件 |", "|---|---:|---:|---|"]
    for row in result["pairs"]:
        label = "同账号原生通道" if row["same_account_native_comparison"] else "探索性；账号或执行环境不等价"
        lines.append(f"| {row['left']} / {row['right']} | {row['left_wins']} / {row['right_wins']} / {row['ties']} | {row['unpaired']} | {label} |")
    lines += ["", "## 解释边界", "", "本报告分别统计协议完成和固定答案评分，不证明普遍质量提升或‘满血恢复’。", ""]
    lines.extend("- " + limit for limit in result["limits"])
    lines += ["", "账号指纹、每次提交的终态、窗口时间和停用原因见 report.json。原始回答、加密推理和凭据不写入报告。", ""]
    (ledger.directory / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return result

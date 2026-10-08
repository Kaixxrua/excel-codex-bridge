"""Deterministic synthetic fixtures with local oracles; never execute model code."""

import copy
import json
import random
import sqlite3

INSTRUCTIONS = "Solve the provided synthetic task. Follow its output format exactly. Do not use outside information."


def generate(profile: str, seed: int) -> list[dict]:
    rng = random.Random(seed)
    tasks = []
    families = ["records", "lookup"] if profile == "smoke" else ["records", "schedule", "sql", "lookup"]
    variants = 1 if profile == "smoke" else 2 if profile == "screen" else 4
    for variant in range(variants):
        for family in families:
            task = {"id": f"{family}-{variant + 1}", "family": family, "max_turns": 1, "tools": []}
            if family == "records":
                rows = [{"id": f"r{i}", "active": rng.choice([True, False]), "value": rng.randrange(10, 100)} for i in range(9)]
                expected = [r["id"] for r in sorted(rows, key=lambda r: (-r["value"], r["id"])) if r["active"]]
                task.update(prompt="Return only JSON {\"ids\":[...]}, containing active record IDs sorted by value descending, then ID ascending. Records: " + json.dumps(rows),
                            expected={"ids": expected})
            elif family == "schedule":
                durations = [rng.randrange(2, 10) for _ in range(7)]
                dependencies = [[], [0], [0], [1, 2], [2], [3, 4], [5]]
                finish = []
                for i, duration in enumerate(durations):
                    finish.append(duration + max((finish[j] for j in dependencies[i]), default=0))
                jobs = [{"id": i, "duration": duration, "after": dependencies[i]} for i, duration in enumerate(durations)]
                task.update(prompt="Jobs may run in parallel on unlimited workers, starting at time 0 after all dependencies finish. Return only JSON {\"finish\":[...]}, with earliest finish times in ID order. Jobs: " + json.dumps(jobs),
                            expected={"finish": finish})
            elif family == "sql":
                rows = [[i, rng.choice(["east", "west", "north"]), rng.randrange(1, 20), rng.randrange(2)] for i in range(18)]
                totals = {}
                for _, region, amount, paid in rows:
                    if paid:
                        totals[region] = totals.get(region, 0) + amount
                task.update(prompt="SQLite table sales(id INTEGER, region TEXT, amount INTEGER, paid INTEGER). Return only JSON {\"sql\":\"SELECT ...\"}. Query total amount by region for paid=1, with region then total as columns, ordered by region ascending. Do not use other tables.",
                            rows=rows, expected=[[key, totals[key]] for key in sorted(totals)])
            else:
                key = "record-" + format(rng.getrandbits(48), "012x")
                value = format(rng.getrandbits(80), "020x")
                task.update(prompt=f"Call fixture_read exactly once with key {json.dumps(key)}. Then return only JSON {{\"value\":\"the returned value\"}}.",
                            expected={"value": value}, fixture={"key": key, "value": value}, max_turns=2,
                            tools=[{"type": "function", "name": "fixture_read", "description": "Read one synthetic fixture by its exact key.",
                                    "parameters": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"], "additionalProperties": False}, "strict": True}])
            tasks.append(task)
    return tasks


def payload(task: dict, model: str, effort: str) -> dict:
    body = {"model": model, "instructions": INSTRUCTIONS, "input": [{"role": "user", "content": task["prompt"]}],
            "reasoning": {"effort": effort}, "stream": True, "store": False, "include": ["reasoning.encrypted_content"]}
    if task["tools"]:
        body["tools"] = copy.deepcopy(task["tools"])
    return body


def continue_tool(task: dict, output: list, seen: set[str]) -> list:
    calls = [item for item in output if item.get("type") == "function_call"]
    if len(calls) != 1 or task["family"] != "lookup":
        raise ValueError("Unexpected tool calls")
    call = calls[0]
    if (call.get("name") != "fixture_read" or call.get("namespace") not in (None, "functions")
            or not isinstance(call.get("call_id"), str) or not call["call_id"] or call["call_id"] in seen
            or json.loads(call.get("arguments", "null")) != {"key": task["fixture"]["key"]}):
        raise ValueError("Tool call does not match the frozen fixture")
    for item in output:
        if item.get("type") not in {"function_call", "reasoning", "message"}:
            raise ValueError("Unsupported continuation item")
        if item.get("type") == "reasoning" and not item.get("encrypted_content"):
            raise ValueError("Missing encrypted continuation")
    seen.add(call["call_id"])
    return copy.deepcopy(output) + [{"type": "function_call_output", "call_id": call["call_id"],
                                    "output": json.dumps({"value": task["fixture"]["value"]})}]


def sql_result(query: str, rows: list) -> list:
    if not isinstance(query, str) or len(query) > 12000:
        raise ValueError("Invalid SQL")
    db = sqlite3.connect(":memory:")
    try:
        db.execute("CREATE TABLE sales(id INTEGER,region TEXT,amount INTEGER,paid INTEGER)")
        db.executemany("INSERT INTO sales VALUES(?,?,?,?)", rows)
        def authorize(action, arg1, arg2, database, trigger):
            allowed = action == sqlite3.SQLITE_SELECT or (action == sqlite3.SQLITE_READ and arg1 == "sales")
            allowed |= action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in {"sum", "count", "min", "max", "avg", "coalesce", "round"}
            return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY
        db.set_authorizer(authorize)
        steps = 0
        def progress():
            nonlocal steps
            steps += 1
            return int(steps > 100)
        db.set_progress_handler(progress, 1000)
        return [list(row) for row in db.execute(query).fetchmany(101)]
    finally:
        db.close()


def grade(task: dict, answer: str, tool_calls: int) -> dict:
    try:
        value = json.loads(answer.strip())
        if task["family"] == "sql":
            passed = isinstance(value, dict) and set(value) == {"sql"} and sql_result(value["sql"], task["rows"]) == task["expected"]
        else:
            passed = value == task["expected"] and (task["family"] != "lookup" or tool_calls == 1)
    except (ValueError, TypeError, sqlite3.Error):
        passed = False
    return {"passed": bool(passed), "score": int(bool(passed)), "reason": "oracle_match" if passed else "oracle_mismatch"}

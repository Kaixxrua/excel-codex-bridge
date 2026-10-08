"""Credential references and a frozen, counterbalanced experiment manifest."""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path
import random
import re
import platform
from urllib.parse import urlsplit

from . import GRADER_VERSION, SCHEMA, tasks
from .. import __version__
from .storage import Ledger, digest, private_directory, write_json

KINDS = ("codex-http", "codex-ws", "codex-cli", "siwc", "responses-http", "bps")
DEFAULT_ROUTES = ("codex-http", "codex-ws", "codex-cli")
CAPS = {"smoke": 32, "screen": 192, "confirm": 512}
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def endpoint(value: str) -> str:
    if not isinstance(value, str) or any(ord(c) < 33 for c in value):
        raise ValueError("Invalid Responses base URL")
    parts = urlsplit(value)
    if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("Base URL must have a host and contain no credentials, query or fragment")
    try:
        loopback = ipaddress.ip_address(parts.hostname).is_loopback
    except ValueError:
        loopback = parts.hostname == "localhost"
    if parts.scheme != "https" and not (parts.scheme == "http" and loopback):
        raise ValueError("Use HTTPS, or HTTP on a literal loopback address")
    return value.rstrip("/")


def validate(value: dict) -> dict:
    allowed = {"profile", "seed", "routes", "model", "effort", "max_calls", "timeout_seconds", "window_gap_seconds"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("Unknown study fields; use credential references, never embed tokens")
    config = {"profile": "smoke", "seed": 1, "effort": "low", "timeout_seconds": 120, "window_gap_seconds": 3600, **value}
    if config["profile"] not in CAPS:
        raise ValueError("profile must be smoke, screen or confirm")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", config.get("model", "")) or config["model"].endswith("-excel"):
        raise ValueError("Select a native model name shared by the compared channels")
    if config["effort"] not in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}:
        raise ValueError("Unsupported reasoning effort; it will never be silently downgraded")
    for key, lower, upper in (("seed", 0, 2**63 - 1), ("timeout_seconds", 1, 600), ("window_gap_seconds", 60, 604800)):
        if type(config[key]) is not int or not lower <= config[key] <= upper:
            raise ValueError("Invalid " + key)
    routes = config.get("routes")
    if not isinstance(routes, list) or not 1 <= len(routes) <= 8:
        raise ValueError("Select between one and eight explicit routes")
    seen = set()
    for route in routes:
        if not isinstance(route, dict) or set(route) - {"id", "kind", "auth_file", "executable", "base_url", "key_env", "key_file"}:
            raise ValueError("Unknown route fields; credentials must be references")
        if not IDENTIFIER.fullmatch(route.get("id", "")) or route["id"] in seen or route.get("kind") not in KINDS:
            raise ValueError("Each route requires a unique id and supported kind")
        seen.add(route["id"])
        kind = route["kind"]
        extras = set(route) - {"id", "kind"}
        legal = {"base_url", "key_env", "key_file"} if kind == "responses-http" else {"auth_file", "executable"} if kind == "codex-cli" else {"auth_file"}
        if extras - legal:
            raise ValueError("Credential or endpoint override does not belong to this route")
        for key in ("auth_file", "executable", "key_file"):
            if key in route:
                path = Path(route[key]).expanduser()
                if not path.is_absolute():
                    raise ValueError(key + " must be an absolute path")
                route[key] = str(path)
        if kind == "responses-http":
            route["base_url"] = endpoint(route.get("base_url", ""))
            if ("key_env" in route) == ("key_file" in route):
                raise ValueError("Responses routes require exactly one key_env or key_file reference")
            if "key_env" in route and not ENV_NAME.fullmatch(route["key_env"]):
                raise ValueError("Invalid key environment variable name")
    count = sum(t["max_turns"] for t in tasks.generate(config["profile"], config["seed"])) * len(routes)
    config.setdefault("max_calls", count * (2 if config["profile"] == "confirm" else 1))
    if type(config["max_calls"]) is not int or not 1 <= config["max_calls"] <= CAPS[config["profile"]]:
        raise ValueError("Call budget exceeds this profile's limit")
    return config


def prepare(config: dict, directory: Path) -> dict:
    config = validate(config)
    generated = tasks.generate(config["profile"], config["seed"])
    rng, schedule = random.Random(config["seed"] ^ 0xC0DE), []
    for window in range(1, 3 if config["profile"] == "confirm" else 2):
        ordered = generated.copy()
        rng.shuffle(ordered)
        for task in ordered:
            routes = [route["id"] for route in config["routes"]]
            rng.shuffle(routes)
            for route in routes:
                schedule.append({"arm": f"w{window}:{task['id']}:{route}", "window": window, "task": task["id"], "route": route})
    manifest = {"schema": SCHEMA, "grader_version": GRADER_VERSION, "tool_version": __version__,
                "runtime": {"system": platform.system(), "machine": platform.machine(), "python": platform.python_version()},
                "config": config, "tasks": generated, "schedule": schedule}
    private_directory(directory, new=True)
    write_json(directory / "manifest.json", manifest, new=True)
    ledger = Ledger(directory, manifest, create=True)
    ledger.close()
    return {"study": str(directory.resolve()), "manifest_hash": digest(manifest), "max_calls": config["max_calls"], "scheduled_arms": len(schedule)}


def open_study(directory: Path):
    if directory.is_symlink():
        raise ValueError("Study directory cannot be a symlink")
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != SCHEMA or manifest.get("grader_version") != GRADER_VERSION:
        raise ValueError("Unsupported frozen study version")
    validate(manifest["config"])
    return manifest, Ledger(directory, manifest)

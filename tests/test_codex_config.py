from __future__ import annotations

import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path, PureWindowsPath

from excel_codex_bridge import codex_config, excel_upstream
from excel_codex_bridge import cli
from excel_codex_bridge.cli import _no_proxy_env, main


def parse_overrides(args: list[str]) -> dict:
    """Parse ``-c key=value`` pairs the way Codex does (value as TOML)."""
    parsed: dict = {}
    for flag, pair in zip(args[0::2], args[1::2]):
        if flag != "-c":
            continue
        key, value = pair.split("=", 1)
        parsed[key] = tomllib.loads(f"v = {value}")["v"]
    return parsed


class CatalogTests(unittest.TestCase):
    def test_catalog_lists_the_three_excel_models_text_only(self):
        models = codex_config.catalog_payload()["models"]
        self.assertEqual({m["slug"] for m in models}, set(excel_upstream.MODEL_IDS))
        for model in models:
            self.assertEqual(model["input_modalities"], ["text"])
            self.assertFalse(model["supports_parallel_tool_calls"])
            self.assertLess(model["auto_compact_token_limit"], model["context_window"])
            efforts = [level["effort"] for level in model["supported_reasoning_levels"]]
            self.assertEqual(efforts, ["low", "medium", "high", "xhigh"])
        luna = next(m for m in models if m["slug"] == "gpt-5.6-luna-excel")
        self.assertEqual(luna["context_window"], 200_000)
        astra = next(m for m in models if m["slug"] == "gpt-6-astra-excel")
        self.assertIn("experimental", astra["description"])

    def test_catalog_order_covers_every_served_model(self):
        self.assertEqual(sorted(codex_config.CATALOG_ORDER), sorted(excel_upstream.MODEL_IDS))

    def test_write_catalog_is_valid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = codex_config.write_catalog(Path(directory))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), codex_config.catalog_payload())


class OverrideTests(unittest.TestCase):
    def test_overrides_route_one_session_through_the_bridge(self):
        catalog = Path("/tmp/catalog.json")
        args = codex_config.codex_overrides(4321, catalog)
        parsed = parse_overrides(args)
        self.assertEqual(parsed["model"], "gpt-5.6-sol-excel")
        self.assertEqual(parsed["model_provider"], "excel-bridge")
        self.assertEqual(parsed["model_providers.excel-bridge.base_url"], "http://127.0.0.1:4321/v1")
        self.assertEqual(parsed["model_providers.excel-bridge.wire_api"], "responses")
        self.assertEqual(parsed["model_catalog_json"], str(catalog))

    def test_windows_paths_survive_toml_parsing(self):
        path = PureWindowsPath(r"C:\Users\张三\AppData\Local\excel-codex-bridge\codex-model-catalog.json")
        parsed = parse_overrides(codex_config.codex_overrides(1, path))
        self.assertEqual(parsed["model_catalog_json"], str(path))

    def test_overrides_follow_model_subcommands(self):
        # Codex ignores root-level -c once a subcommand has its own -c, which
        # would silently route the session to api.openai.com instead.
        overrides = ["-c", "model_provider='excel-bridge'"]
        for sub in ("exec", "e", "review", "resume", "fork"):
            with self.subTest(sub=sub):
                command = codex_config.codex_command("codex", overrides, [sub, "-c", "x=1", "hi"])
                self.assertEqual(command, ["codex", sub, *overrides, "-c", "x=1", "hi"])
        self.assertEqual(
            codex_config.codex_command("codex", overrides, ["fix the bug"]),
            ["codex", *overrides, "fix the bug"],
        )
        self.assertEqual(codex_config.codex_command("codex", overrides, []), ["codex", *overrides])

    def test_config_snippet_is_valid_toml(self):
        snippet = codex_config.config_snippet(8765, Path(r"C:\x\catalog.json"))
        config = tomllib.loads(snippet)
        self.assertEqual(config["model_provider"], "excel-bridge")
        self.assertEqual(config["model_providers"]["excel-bridge"]["base_url"], "http://127.0.0.1:8765/v1")


class CliTests(unittest.TestCase):
    def test_no_proxy_env_adds_loopback_once(self):
        env = _no_proxy_env({"NO_PROXY": "corp.local,localhost"})
        self.assertEqual(env["NO_PROXY"], "corp.local,localhost,127.0.0.1")
        self.assertEqual(env["no_proxy"], "127.0.0.1,localhost")

    def test_package_path_reaches_the_bridge_but_not_codex(self):
        root = cli._PACKAGE_ROOT
        user = os.path.join(os.sep, "work", "lib")
        started = {"PYTHONPATH": os.pathsep.join([root, user])}
        self.assertEqual(cli._bridge_env(dict(started))["PYTHONPATH"], os.pathsep.join([root, user]))
        self.assertEqual(cli._codex_env(dict(started))["PYTHONPATH"], user)
        self.assertNotIn("PYTHONPATH", cli._codex_env({"PYTHONPATH": root + os.pathsep}))
        self.assertEqual(cli._bridge_env({})["PYTHONPATH"], root)

    def test_serve_refuses_non_loopback_host(self):
        self.assertEqual(main(["serve", "--host", "0.0.0.0"]), 2)


if __name__ == "__main__":
    unittest.main()

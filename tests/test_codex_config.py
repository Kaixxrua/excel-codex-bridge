from __future__ import annotations

import json
import os
import tempfile
import tomllib
import unittest
from pathlib import Path, PureWindowsPath
from unittest import mock

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


IMAGE_TOOL = {"x-openai-actor-authorization": "excel-codex-bridge"}


OFFICIAL = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-6-sol", "gpt-6-luna", "gpt-6-astra")


class CatalogTests(unittest.TestCase):
    def test_catalog_lists_the_excel_models_with_pictures(self):
        models = [m for m in codex_config.catalog_payload()["models"] if m["visibility"] == "list"]
        # OpenAI's own names where it has them, so conversations carry on with the bridge off.
        self.assertEqual(
            {m["slug"] for m in models},
            set(OFFICIAL) | {m for m in excel_upstream.MODEL_IDS if m.endswith("-1m-excel")},
        )
        for model in models:
            self.assertEqual(model["input_modalities"], ["text", "image"])
            self.assertTrue(model["supports_parallel_tool_calls"])
            self.assertLess(model["auto_compact_token_limit"], model["context_window"])
            efforts = [level["effort"] for level in model["supported_reasoning_levels"]]
            self.assertEqual(efforts[:4], ["low", "medium", "high", "xhigh"])
        windows = {m["slug"]: m["context_window"] for m in models}
        self.assertEqual(len(windows), 12)
        for base in ("5.6-sol", "5.6-terra", "5.6-luna", "6-sol", "6-luna", "6-astra"):
            with self.subTest(base=base):
                self.assertEqual(windows[f"gpt-{base}"], 500_000)
                self.assertEqual(windows[f"gpt-{base}-1m-excel"], 918_000)
        self.assertEqual(
            {m["slug"]: m["display_name"] for m in models if m["slug"].startswith("gpt-6-sol")},
            {"gpt-6-sol": "6-Sol Excel", "gpt-6-sol-1m-excel": "6-Sol Excel 1M"},
        )
        self.assertFalse(any("experimental" in m["description"] for m in models))

    def test_ultra_where_codex_has_it(self):
        # Codex sends ultra as multi_agent_reasoning_effort and hands work to helpers
        # unasked (multi-agent v2); the backend goes no deeper than xhigh.
        models = {m["slug"]: m for m in codex_config.catalog_payload()["models"]}
        for slug, model in models.items():
            with self.subTest(slug=slug):
                efforts = [level["effort"] for level in model["supported_reasoning_levels"]]
                if "-luna" in slug:
                    self.assertEqual(efforts, ["low", "medium", "high", "xhigh"])
                    self.assertNotIn("multi_agent_version", model)
                    self.assertNotIn("multi_agent_reasoning_effort", model)
                else:
                    self.assertEqual(efforts, ["low", "medium", "high", "xhigh", "ultra"])
                    self.assertEqual(model["multi_agent_version"], "v2")
                    self.assertEqual(model["multi_agent_reasoning_effort"], "xhigh")
        self.assertEqual(
            sorted(slug for slug, model in models.items() if model["visibility"] == "list"
                   and "multi_agent_version" in model),
            ["gpt-5.6-sol", "gpt-5.6-sol-1m-excel", "gpt-5.6-terra", "gpt-5.6-terra-1m-excel",
             "gpt-6-astra", "gpt-6-astra-1m-excel", "gpt-6-sol", "gpt-6-sol-1m-excel"],
        )

    def test_old_names_stay_for_conversations_started_with_them(self):
        models = {m["slug"]: m for m in codex_config.catalog_payload()["models"]}
        self.assertEqual(len(models), 18)
        for base in OFFICIAL:
            with self.subTest(base=base):
                old = models[f"{base}-excel"]
                self.assertEqual(old["visibility"], "hide")
                self.assertEqual({**old, "slug": base, "visibility": "list", "priority": 0},
                                 {**models[base], "priority": 0})

    def test_codex_model_names(self):
        self.assertEqual(codex_config.codex_model("gpt-6-sol-excel"), "gpt-6-sol")
        self.assertEqual(codex_config.codex_model("GPT-6-Sol-Excel"), "gpt-6-sol")
        self.assertEqual(codex_config.codex_model("gpt-6-sol-1m-excel"), "gpt-6-sol-1m-excel")
        self.assertEqual(codex_config.codex_model("gpt-6-sol"), "gpt-6-sol")
        self.assertEqual(codex_config.codex_model("something-else"), "something-else")
        self.assertEqual(codex_config.DEFAULT_MODEL, "gpt-5.6-sol")

    def test_long_context_aliases_compact_near_their_window(self):
        models = {m["slug"]: m for m in codex_config.catalog_payload()["models"]}
        self.assertEqual(models["gpt-6-sol"]["auto_compact_token_limit"], 450_000)
        self.assertEqual(models["gpt-6-sol"]["max_context_window"], 500_000)
        self.assertEqual(models["gpt-6-sol-1m-excel"]["auto_compact_token_limit"], 826_000)
        self.assertEqual(models["gpt-6-sol-1m-excel"]["max_context_window"], 918_000)
        self.assertIn("918,000 token context", models["gpt-6-sol-1m-excel"]["description"])

    def test_catalog_defers_app_and_mcp_tools_behind_tool_search(self):
        # As OpenAI's catalog has it: the bridge handles tool_search, so Codex
        # keeps app and MCP tools out of each request until a search loads them.
        for model in codex_config.catalog_payload()["models"]:
            with self.subTest(slug=model["slug"]):
                self.assertIs(model["supports_search_tool"], True)
                # Codex's notes on using skills and apps are the only ones these models get.
                self.assertNotIn("include_skills_usage_instructions", model)
                self.assertNotIn("include_apps_usage_instructions", model)

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
        self.assertEqual(parsed["model"], "gpt-5.6-sol")
        self.assertEqual(parsed["model_provider"], "excel-bridge")
        self.assertNotIn("openai_base_url", parsed)
        self.assertEqual(parsed["model_providers.excel-bridge.base_url"], "http://127.0.0.1:4321/v1")
        self.assertEqual(parsed["model_providers.excel-bridge.wire_api"], "responses")
        # The header Codex wants before it offers its image tool to a provider.
        self.assertEqual(parsed["model_providers.excel-bridge.http_headers"], IMAGE_TOOL)
        self.assertEqual(parsed["model_catalog_json"], str(catalog))

    def test_shared_overrides_stand_in_for_codex_own_provider(self):
        catalog = Path("/tmp/catalog.json")
        parsed = parse_overrides(codex_config.codex_overrides(4321, catalog, "gpt-6-luna-excel", shared=True))
        self.assertEqual(parsed["model_provider"], "openai")
        self.assertEqual(parsed["openai_base_url"], "http://127.0.0.1:4321/v1")
        self.assertEqual(parsed["model"], "gpt-6-luna")
        # Conversations started as excel-bridge can still be resumed through the bridge.
        self.assertEqual(parsed["model_providers.excel-bridge.base_url"], "http://127.0.0.1:4321/v1")

    def test_signed_in_looks_only_at_the_kind_of_sign_in(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.assertFalse(codex_config.codex_signed_in(home))
            for content, expected in (
                ({"tokens": {"access_token": "x", "refresh_token": "y"}}, True),
                ({"auth_mode": "apikey", "OPENAI_API_KEY": "sk-x"}, True),
                ({"tokens": {"access_token": " "}}, False),
                ({"OPENAI_API_KEY": None, "tokens": None}, False),
                ([], False),
            ):
                with self.subTest(content=content):
                    (home / "auth.json").write_text(json.dumps(content), encoding="utf-8")
                    self.assertEqual(codex_config.codex_signed_in(home), expected)
            (home / "auth.json").write_text("{not json", encoding="utf-8")
            self.assertFalse(codex_config.codex_signed_in(home))
            with mock.patch.dict(os.environ, {"CODEX_HOME": directory}):
                (home / "auth.json").write_text('{"OPENAI_API_KEY": "sk-x"}', encoding="utf-8")
                self.assertTrue(codex_config.codex_signed_in())

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
        self.assertEqual(config["model_providers"]["excel-bridge"]["http_headers"], IMAGE_TOOL)


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

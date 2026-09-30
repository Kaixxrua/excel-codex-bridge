from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from excel_codex_bridge import __version__, cli, codex_config, codex_threads, desktop_config

from helpers import write_webview_session

CATALOG = Path(r"C:\Users\张三\AppData\Local\excel-codex-bridge\codex-model-catalog.json")
USER_CONFIG = """\
# my settings
model = "gpt-5.5"
model_provider = "openai"
approval_policy = "on-request"

[model_providers.excel-bridge]
name = "old paste from print-config"
base_url = "http://127.0.0.1:9999/v1"

[history]
persistence = "none"
"""


def enable(text: str, **kwargs) -> str:
    return desktop_config.enable(text, port=8765, catalog=CATALOG, model="gpt-5.6-sol-excel", **kwargs)


class EnableTests(unittest.TestCase):
    def assert_points_at_bridge(self, text: str) -> dict:
        data = tomllib.loads(text)
        self.assertEqual(data["model_provider"], "excel-bridge")
        self.assertNotIn("openai_base_url", data)
        self.assertEqual(data["model"], "gpt-5.6-sol")
        self.assertEqual(data["model_catalog_json"], str(CATALOG))
        provider = data["model_providers"]["excel-bridge"]
        self.assertEqual(provider["base_url"], "http://127.0.0.1:8765/v1")
        self.assertEqual(provider["wire_api"], "responses")
        self.assertEqual(provider["http_headers"], {"x-openai-actor-authorization": "excel-codex-bridge"})
        return data

    def test_user_settings_survive_and_come_back_exactly(self):
        enabled = enable(USER_CONFIG)
        data = self.assert_points_at_bridge(enabled)
        self.assertEqual(data["approval_policy"], "on-request")
        self.assertEqual(data["history"], {"persistence": "none"})
        self.assertIn(desktop_config.DISABLED_PREFIX + 'model = "gpt-5.5"', enabled)
        self.assertEqual(desktop_config.strip_managed(enabled), USER_CONFIG)

    def test_empty_config(self):
        enabled = enable("")
        self.assert_points_at_bridge(enabled)
        self.assertEqual(desktop_config.strip_managed(enabled), "")

    def test_enabling_twice_is_the_same_as_once(self):
        once = enable(USER_CONFIG)
        self.assertEqual(enable(once), once)
        self.assertEqual(desktop_config.strip_managed(enable(once)), USER_CONFIG)

    def test_crlf_and_missing_final_newline_are_kept(self):
        for original in (USER_CONFIG.replace("\n", "\r\n"), USER_CONFIG.rstrip("\n"), 'model = "x"'):
            with self.subTest(original=original[-20:]):
                enabled = enable(original)
                self.assert_points_at_bridge(enabled)
                if "\r\n" in original:
                    self.assertNotIn("\n", enabled.replace("\r\n", ""))
                self.assertEqual(desktop_config.strip_managed(enabled), original)

    def test_own_provider_sub_tables_are_set_aside_too(self):
        # A hand-made image-tool setup, as users pasted it before the bridge set the header.
        original = (
            "[model_providers.excel-bridge]\n"
            'base_url = "http://127.0.0.1:8765/v1"\n'
            "[model_providers.excel-bridge.http_headers]\n"
            '"x-openai-actor-authorization" = "local-image-extension"\n'
            "[model_providers.excel-bridge-old]\n"
            'name = "kept"\n'
        )
        enabled = enable(original)
        data = self.assert_points_at_bridge(enabled)
        self.assertEqual(data["model_providers"]["excel-bridge-old"], {"name": "kept"})
        self.assertEqual(desktop_config.strip_managed(enabled), original)

    def test_shared_mode_stands_in_for_codex_own_provider(self):
        original = USER_CONFIG.replace("[history]", 'openai_base_url = "https://example.test/v1"\n\n[history]')
        original = 'openai_base_url = "https://proxy.test/v1"\n' + original
        for text in (original, original.replace("\n", "\r\n"), original.rstrip("\n")):
            with self.subTest(text=text[-12:]):
                enabled = enable(text, shared=True)
                data = tomllib.loads(enabled)
                self.assertEqual(data["model_provider"], "openai")
                self.assertEqual(data["openai_base_url"], "http://127.0.0.1:8765/v1")
                self.assertEqual(data["model"], "gpt-5.6-sol")
                # Still defined, for conversations started as excel-bridge.
                self.assertEqual(data["model_providers"]["excel-bridge"]["base_url"], "http://127.0.0.1:8765/v1")
                self.assertIn(desktop_config.DISABLED_PREFIX + 'openai_base_url = "https://proxy.test/v1"', enabled)
                self.assertEqual(desktop_config.strip_managed(enabled), text)
                # Switching modes (signed in or out in between) still comes back exactly.
                self.assertEqual(desktop_config.strip_managed(enable(enabled)), text)
                self.assertEqual(enable(enable(text), shared=True), enabled)

    def test_nested_model_keys_are_left_alone(self):
        original = '[profiles.fast]\nmodel = "gpt-5.5"\n'
        enabled = enable(original)
        self.assertNotIn(desktop_config.DISABLED_PREFIX, enabled)
        self.assertEqual(tomllib.loads(enabled)["profiles"]["fast"]["model"], "gpt-5.5")

    def assert_quiet(self, original: str, enabled: str) -> dict:
        data = self.assert_points_at_bridge(enabled)
        self.assertIs(data["features"]["apps"], False)
        self.assertIs(data["features"]["remote_plugin"], False)
        self.assertTrue(desktop_config.features_quiet(enabled))
        self.assertEqual(desktop_config.strip_managed(enabled), original)
        # Again, with and without: the same text, and back to no change to the features.
        self.assertEqual(enable(enabled, quiet_features=True), enabled)
        self.assertFalse(desktop_config.features_quiet(enable(enabled)))
        self.assertEqual(desktop_config.strip_managed(enable(enabled)), original)
        return data

    def test_apps_and_plugin_suggestions_can_be_turned_off(self):
        for original in (USER_CONFIG, USER_CONFIG.replace("\n", "\r\n"), USER_CONFIG.rstrip("\n"), ""):
            with self.subTest(original=original[-12:]):
                enabled = enable(original, quiet_features=True)
                self.assertIn("features.apps = false", enabled)
                self.assert_quiet(original, enabled)
                self.assertNotIn("features", tomllib.loads(enable(original)))

    def test_the_users_own_features_table_gets_them(self):
        table = '[features]\napps = true\n"remote_plugin" = true\nweb_search = true\n'
        for original in (
            USER_CONFIG + table,
            (table + USER_CONFIG).replace("\n", "\r\n"),
            USER_CONFIG + "[features] # mine\nweb_search = true",
            USER_CONFIG + "[features]",
            'features.apps = true\nfeatures.web_search = true\n' + USER_CONFIG,
        ):
            with self.subTest(original=original[-30:]):
                enabled = enable(original, quiet_features=True)
                data = self.assert_quiet(original, enabled)
                if "web_search" in original:
                    self.assertIs(data["features"]["web_search"], True)
                if "apps = true" in original:
                    self.assertIn(desktop_config.DISABLED_PREFIX + "apps = true", enabled.replace("features.", ""))

    def test_features_elsewhere_are_left_alone(self):
        original = '[profiles.fast.features]\napps = true\n'
        data = tomllib.loads(enable(original, quiet_features=True))
        self.assertIs(data["profiles"]["fast"]["features"]["apps"], True)
        self.assertIs(data["features"]["apps"], False)
        # An inline table cannot be added to.
        inline = 'features = { web_search = true }\n'
        self.assertEqual(enable(inline, quiet_features=True), enable(inline))
        self.assertFalse(desktop_config.features_quiet(enable(inline)))

    def test_active_profile_with_its_own_model_is_reported(self):
        self.assertEqual(
            desktop_config.profile_override('profile = "fast"\n[profiles.fast]\nmodel = "o3"\n'), "fast"
        )
        self.assertIsNone(desktop_config.profile_override('profile = "fast"\n[profiles.fast]\nx = 1\n'))
        self.assertIsNone(desktop_config.profile_override(USER_CONFIG))


class FileTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "config.toml"

    def enable_file(self):
        return desktop_config.enable_file(self.path, port=8765, catalog=CATALOG, model="gpt-5.6-sol-excel")

    def test_round_trip_keeps_bytes_and_a_backup(self):
        original = ("\ufeff" + USER_CONFIG.replace("\n", "\r\n")).encode("utf-8")
        self.path.write_bytes(original)
        backup = self.enable_file()
        self.assertEqual(backup.read_bytes(), original)
        self.assertTrue(desktop_config.is_enabled(self.path.read_text(encoding="utf-8-sig")))
        # A second enable (say after a crash) keeps the first backup.
        self.assertIsNone(self.enable_file())
        self.assertEqual(backup.read_bytes(), original)
        self.assertTrue(desktop_config.disable_file(self.path))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(desktop_config.disable_file(self.path))

    def test_missing_config_is_created_and_emptied(self):
        self.assertIsNone(self.enable_file())
        self.assertTrue(desktop_config.disable_file(self.path))
        self.assertEqual(self.path.read_text(), "")

    def test_shared_round_trip(self):
        self.path.write_text(USER_CONFIG)
        desktop_config.enable_file(self.path, port=8765, catalog=CATALOG, model="gpt-6-sol-excel", shared=True)
        data = tomllib.loads(self.path.read_text())
        self.assertEqual((data["model_provider"], data["model"]), ("openai", "gpt-6-sol"))
        self.assertTrue(desktop_config.disable_file(self.path))
        self.assertEqual(self.path.read_text(), USER_CONFIG)

    def test_quiet_round_trip(self):
        original = (USER_CONFIG + "[features]\napps = true\n").replace("\n", "\r\n").encode()
        self.path.write_bytes(original)
        desktop_config.enable_file(self.path, port=8765, catalog=CATALOG, model="gpt-6-sol-excel",
                                   quiet_features=True)
        self.assertTrue(desktop_config.features_quiet(self.path.read_text()))
        self.assertTrue(desktop_config.disable_file(self.path))
        self.assertEqual(self.path.read_bytes(), original)

    def test_features_that_cannot_be_turned_off_do_not_stop_the_rest(self):
        # `features.apps` as a table of its own, and an inline table: TOML allows no addition to either.
        for original in ('[features.apps]\nenabled = true\n', 'features = { apps = true }\n'):
            with self.subTest(original=original):
                self.path.write_text(original)
                desktop_config.enable_file(self.path, port=8765, catalog=CATALOG, model="gpt-6-sol-excel",
                                           quiet_features=True)
                text = self.path.read_text()
                self.assertEqual(tomllib.loads(text)["model_provider"], "excel-bridge")
                self.assertFalse(desktop_config.features_quiet(text))
                self.assertTrue(desktop_config.disable_file(self.path))
                self.assertEqual(self.path.read_text(), original)

    def test_without_tomllib_our_own_lines_tell(self):
        for original in ("", "[features]\n"):
            with self.subTest(original=original):
                enabled = enable(original, quiet_features=True)
                with mock.patch.object(desktop_config, "_parse", return_value=None):
                    self.assertTrue(desktop_config.features_quiet(enabled))
                    self.assertFalse(desktop_config.features_quiet(enable(original)))

    def test_invalid_toml_is_left_untouched(self):
        self.path.write_text("approval_policy = \n")
        with self.assertRaises(desktop_config.ConfigError):
            self.enable_file()
        self.assertEqual(self.path.read_text(), "approval_policy = \n")


class DesktopCommandTests(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.webview = write_webview_session(root / "webview", time.time() + 3 * 86400)
        self.config = root / "codex-home" / "config.toml"
        self.config.parent.mkdir()
        self.config.write_text(USER_CONFIG)
        env = {"CODEX_HOME": str(self.config.parent), "EXCEL_BRIDGE_HOME": str(root / "bridge-home")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("SIGTERM", "SIGBREAK", "SIGHUP"):
            if hasattr(cli.signal, name):
                sig = getattr(cli.signal, name)
                self.addCleanup(cli.signal.signal, sig, cli.signal.getsignal(sig))
        # This machine's own Codex, if it runs one, is none of these tests' business.
        seen = mock.patch.object(codex_threads, "codex_seen", return_value=False)
        self.codex_seen = seen.start()
        self.addCleanup(seen.stop)

    def run_desktop(self, *extra, while_running=None):
        seen = {}

        def fake_bridge(reader, args, *, host, port, quiet):
            seen["config"] = self.config.read_text()
            seen["port"] = port
            if while_running is not None:
                while_running(seen)
            raise KeyboardInterrupt

        with mock.patch.object(cli, "_run_bridge", fake_bridge), mock.patch.object(cli, "_port_free", lambda p: True):
            code = cli.main(["desktop", "--webview-dir", str(self.webview), "--no-auto-signin", *extra])
        return code, seen

    def test_config_points_at_the_bridge_while_running_and_is_restored(self):
        code, seen = self.run_desktop("--port", "8799")
        self.assertEqual(code, 0)
        self.assertEqual(seen["port"], 8799)
        data = tomllib.loads(seen["config"])
        self.assertEqual(data["model_providers"]["excel-bridge"]["base_url"], "http://127.0.0.1:8799/v1")
        self.assertEqual(self.config.read_text(), USER_CONFIG)

    def test_apps_and_plugin_suggestions_are_off_unless_asked_to_keep_them(self):
        for extra, off in (((), True), (("--keep-apps",), False)):
            with self.subTest(extra=extra), mock.patch.object(cli, "_print") as printed:
                code, seen = self.run_desktop(*extra)
                said = [call.args[0] for call in printed.call_args_list]
                self.assertEqual(code, 0)
                features = tomllib.loads(seen["config"]).get("features", {})
                self.assertEqual(features, {"apps": False, "remote_plugin": False} if off else {})
                self.assertEqual(cli._APPS_OFF in said, off)
                self.assertNotIn(cli._APPS_LEFT_ON, said)
                self.assertEqual(self.config.read_text(), USER_CONFIG)
        self.assertTrue(cli._APPS_OFF.isascii() and cli._APPS_LEFT_ON.isascii())

    def test_features_it_cannot_turn_off_are_said(self):
        original = USER_CONFIG + "[features.apps]\nenabled = true\n"
        self.config.write_text(original)
        with mock.patch.object(cli, "_print") as printed:
            code, seen = self.run_desktop()
        self.assertEqual(code, 0)
        self.assertEqual(tomllib.loads(seen["config"])["model_provider"], "excel-bridge")
        self.assertIn(cli._APPS_LEFT_ON, [call.args[0] for call in printed.call_args_list])
        self.assertEqual(self.config.read_text(), original)

    def test_signed_in_codex_shares_its_conversations(self):
        (self.config.parent / "auth.json").write_text('{"auth_mode": "apikey", "OPENAI_API_KEY": "sk-test"}')
        with mock.patch.object(cli, "_print") as printed:
            code, seen = self.run_desktop("--port", "8799", "--model", "gpt-6-astra-excel")
        self.assertEqual(code, 0)
        data = tomllib.loads(seen["config"])
        self.assertEqual(data["model_provider"], "openai")
        self.assertEqual(data["openai_base_url"], "http://127.0.0.1:8799/v1")
        self.assertEqual(data["model"], "gpt-6-astra")
        self.assertIn(cli._SHARED, [call.args[0] for call in printed.call_args_list])
        self.assertEqual(self.config.read_text(), USER_CONFIG)

    def test_it_says_which_release_it_is(self):
        with mock.patch.object(cli, "_print") as printed:
            code, _ = self.run_desktop()
        self.assertEqual(code, 0)
        said = [call.args[0] for call in printed.call_args_list]
        self.assertIn(f"now use the Excel bridge {__version__} (gpt-5.6-sol).", "\n".join(said))

    def test_it_writes_this_release_s_model_list(self):
        catalog = Path(os.environ["EXCEL_BRIDGE_HOME"]) / "codex-model-catalog.json"
        code, seen = self.run_desktop()
        self.assertEqual(code, 0)
        self.assertEqual(Path(tomllib.loads(seen["config"])["model_catalog_json"]), catalog)
        self.assertEqual(json.loads(catalog.read_text(encoding="utf-8")), codex_config.catalog_payload())

    def test_without_codex_sign_in_the_bridge_is_its_own_provider(self):
        with mock.patch.object(cli, "_print") as printed:
            code, seen = self.run_desktop()
        self.assertEqual(code, 0)
        self.assertEqual(tomllib.loads(seen["config"])["model_provider"], "excel-bridge")
        self.assertIn(cli._SEPARATE, [call.args[0] for call in printed.call_args_list])

    def test_it_says_to_quit_codex_fully_when_done(self):
        for running in (True, False, None):
            with self.subTest(running=running), mock.patch.object(cli, "_print") as printed:
                self.codex_seen.return_value = running
                code, _ = self.run_desktop()
                said = [call.args[0] for call in printed.call_args_list]
                self.assertEqual(code, 0)
                self.assertIn(cli._QUIT_WHEN_DONE, said)
                self.assertIn(cli._REOPEN_AFTER_RESTORE, said)
                # Only when a Codex process is seen: not when the process list cannot be read.
                self.assertEqual(cli._STILL_RUNNING in said, running is True)
                # A Codex already open keeps its old model list; said as this starts, too.
                self.assertEqual(cli._RUNNING_AT_START in said, running is True)
                if running:
                    self.assertLess(said.index(cli._RUNNING_AT_START), said.index(cli._QUIT_WHEN_DONE))
        self.assertIn("tray icon", cli._QUIT_WHEN_DONE)
        self.assertIn("tray icon", cli._RUNNING_AT_START)
        self.assertTrue(cli._RUNNING_AT_START.isascii())
        self.assertIn("os error 10061", cli._REOPEN_AFTER_RESTORE)

    def test_off_says_so_too(self):
        self.run_desktop("--keep-config")
        self.codex_seen.return_value = True
        with mock.patch.object(cli, "_print") as printed:
            self.assertEqual(cli.main(["desktop", "--off"]), 0)
        said = [call.args[0] for call in printed.call_args_list]
        self.assertEqual(said[1:], [cli._REOPEN_AFTER_RESTORE, cli._STILL_RUNNING])

    def test_keep_config_then_off(self):
        code, _ = self.run_desktop("--keep-config")
        self.assertEqual(code, 0)
        self.assertTrue(desktop_config.is_enabled(self.config.read_text()))
        self.assertEqual(cli.main(["desktop", "--off"]), 0)
        self.assertEqual(self.config.read_text(), USER_CONFIG)

    def test_the_windows_timezone_is_put_back_on_the_way_out(self):
        keeper = mock.Mock(**{"put_back.return_value": "China Standard Time"})
        with mock.patch.object(cli, "_keep_windows_timezone", return_value=keeper), \
             mock.patch.object(cli, "_print") as printed:
            code, _ = self.run_desktop()
        self.assertEqual(code, 0)
        keeper.put_back.assert_called_once_with()
        self.assertIn("Windows timezone: put back China Standard Time.",
                      [call.args[0] for call in printed.call_args_list])

    def test_closing_the_window_puts_back_the_timezone(self):
        for extra in ((), ("--keep-config",)):
            with self.subTest(extra=extra):
                self.config.write_text(USER_CONFIG)
                keeper = mock.Mock(**{"put_back.side_effect": ["China Standard Time", None]})
                handlers = []

                def window_closed(seen):
                    # What Windows runs when the console window is closed, before it ends the process.
                    handlers[0]()
                    seen["after"] = self.config.read_text()

                with mock.patch.object(cli, "_keep_windows_timezone", return_value=keeper), \
                     mock.patch.object(cli, "_undo_on_exit", side_effect=handlers.append), \
                     mock.patch.object(cli, "_print"):
                    code, seen = self.run_desktop(*extra, while_running=window_closed)
                self.assertEqual(code, 0)
                # By the handler, then once more (a no-op by then) on the way out.
                self.assertEqual(keeper.put_back.call_count, 2)
                self.assertEqual(seen["after"] == USER_CONFIG, not extra)

    def test_a_timezone_that_cannot_be_put_back_says_what_to_run(self):
        keeper = mock.Mock(**{"put_back.side_effect": RuntimeError("tzutil failed")})
        with mock.patch.object(cli, "_keep_windows_timezone", return_value=keeper), \
             mock.patch.object(cli, "_print") as printed:
            code, _ = self.run_desktop()
        self.assertEqual(code, 0)
        self.assertEqual(self.config.read_text(), USER_CONFIG)
        self.assertTrue(any("tzutil failed" in call.args[0] and "excel-codex timezone restore" in call.args[0]
                            for call in printed.call_args_list))

    def test_busy_port_changes_nothing(self):
        with mock.patch.object(cli, "_port_free", lambda p: False):
            code = cli.main(["desktop", "--webview-dir", str(self.webview), "--no-auto-signin"])
        self.assertEqual(code, 1)
        self.assertEqual(self.config.read_text(), USER_CONFIG)


class OfficialFileTests(unittest.TestCase):
    """`excel-codex restore`: config.toml back on Codex's own setup."""

    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.path = root / "config.toml"
        patcher = mock.patch.dict(os.environ, {"EXCEL_BRIDGE_HOME": str(root / "bridge-home")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def restore(self, text: str | bytes) -> desktop_config.Restored:
        if isinstance(text, str):
            self.path.write_text(text)
        else:
            self.path.write_bytes(text)
        return desktop_config.official_file(self.path)

    def taken_out(self, *lines: str) -> str:
        return "".join(desktop_config.RESTORE_PREFIX + line + "\n" for line in lines)

    def test_what_a_closed_window_left_and_an_old_paste_both_come_out(self):
        original = ("\ufeff" + USER_CONFIG.replace("\n", "\r\n")).encode("utf-8")
        self.path.write_bytes(original)
        desktop_config.enable_file(self.path, port=8765, catalog=CATALOG, model="gpt-6-sol-excel",
                                   shared=True, quiet_features=True)
        on_disk = self.path.read_bytes()
        restored = desktop_config.official_file(self.path)
        self.assertTrue(restored.blocks)
        self.assertEqual(restored.took_out, ["[model_providers.excel-bridge] (the whole table)"])
        self.assertEqual(restored.backup.read_bytes(), on_disk)
        raw = self.path.read_bytes()
        self.assertTrue(raw.startswith("\ufeff".encode("utf-8")))
        text = raw.decode("utf-8-sig")
        self.assertNotIn("\n", text.replace("\r\n", ""))
        data = tomllib.loads(text)
        self.assertEqual((data["model"], data["model_provider"]), ("gpt-5.5", "openai"))
        self.assertNotIn("model_providers", data)
        self.assertNotIn("features", data)
        self.assertIn(desktop_config.RESTORE_PREFIX + "[model_providers.excel-bridge]\r\n", text)
        # Only its lines: everything else is the user's, as it was.
        self.assertEqual(text.replace(desktop_config.RESTORE_PREFIX, ""), USER_CONFIG.replace("\n", "\r\n"))

    def test_a_hand_made_shared_setup(self):
        catalog = Path(os.environ["EXCEL_BRIDGE_HOME"]) / codex_config.CATALOG_NAME
        text = (
            'model_provider = "openai"\n'
            'openai_base_url = "http://127.0.0.1:8766/v1"\n'
            f"model_catalog_json = '{catalog}'\n"
            'model = "gpt-6-sol"\n'
        )
        restored = self.restore(text)
        self.assertFalse(restored.blocks)
        self.assertEqual(restored.took_out, ['openai_base_url = "http://127.0.0.1:8766/v1"',
                                             f"model_catalog_json = '{catalog}'"])
        self.assertEqual(restored.kept, [])
        self.assertEqual(self.path.read_text(), (
            'model_provider = "openai"\n'
            + self.taken_out('openai_base_url = "http://127.0.0.1:8766/v1"', f"model_catalog_json = '{catalog}'")
            + 'model = "gpt-6-sol"\n'
        ))

    def test_the_bridge_s_model_list_wherever_it_is(self):
        for value in (r"C:\Users\someone\AppData\Local\excel-codex-bridge\codex-model-catalog.json",
                      "/Users/someone/.excel-codex-bridge/codex-model-catalog.json"):
            with self.subTest(value=value):
                restored = self.restore(f"model_catalog_json = '{value}'\n")
                self.assertEqual(restored.took_out, [f"model_catalog_json = '{value}'"])
        restored = self.restore("model_catalog_json = '/home/someone/models.json'\n")
        self.assertFalse(restored.changed)

    def test_a_loopback_openai_base_url_on_its_own(self):
        # Some other local proxy: left in, and said.
        restored = self.restore('openai_base_url = "http://localhost:9000/v1"\n')
        self.assertFalse(restored.changed)
        self.assertEqual(restored.kept, ['openai_base_url = "http://localhost:9000/v1"'])
        self.assertIsNone(restored.backup)
        # The bridge's default address.
        restored = self.restore('openai_base_url = "http://127.0.0.1:8765/v1/"\n')
        self.assertEqual(restored.took_out, ['openai_base_url = "http://127.0.0.1:8765/v1/"'])

    def test_a_relay_is_left_alone_and_its_login_not_shown(self):
        text = 'model_provider = "OpenAI"\nopenai_base_url = "https://user:secret@relay.example/v1"\n'
        restored = self.restore(text)
        self.assertFalse(restored.changed)
        self.assertEqual(restored.kept, ['model_provider = "OpenAI"', 'openai_base_url = "https://relay.example/v1"'])
        self.assertEqual(self.path.read_text(), text)
        self.assertFalse(self.path.with_name("config.toml" + desktop_config.RESTORE_BACKUP_SUFFIX).exists())

    def test_profiles_and_dotted_keys(self):
        text = (
            'model_providers.excel-bridge.base_url = "http://127.0.0.1:8765/v1"\n'
            'approval_policy = "never"\n'
            "\n"
            "[profiles.long]\n"
            'model = "gpt-6-luna-1m-excel"\n'
            'model_provider = "excel-bridge"\n'
            'model_reasoning_effort = "high"\n'
        )
        restored = self.restore(text)
        self.assertEqual(restored.took_out, ["model_providers.excel-bridge.base_url = ...",
                                             'model = "gpt-6-luna-1m-excel"', 'model_provider = "excel-bridge"'])
        data = tomllib.loads(self.path.read_text())
        self.assertEqual(data, {"approval_policy": "never", "profiles": {"long": {"model_reasoning_effort": "high"}}})

    def test_blank_lines_and_comments_in_its_table_stay(self):
        text = '[model_providers.excel-bridge]\n# pasted\nbase_url = "x"\n\n[model_providers.excel-bridge.http_headers]\na = "b"\n'
        restored = self.restore(text)
        self.assertEqual(restored.took_out, ["[model_providers.excel-bridge] (the whole table)",
                                             "[model_providers.excel-bridge.http_headers] (the whole table)"])
        self.assertEqual(self.path.read_text(), (
            self.taken_out("[model_providers.excel-bridge]") + "# pasted\n" + self.taken_out('base_url = "x"')
            + "\n" + self.taken_out("[model_providers.excel-bridge.http_headers]", 'a = "b"')
        ))

    def test_what_it_cannot_take_out_is_left_and_said(self):
        text = 'model_provider = "excel-bridge"\nmodel_providers = { excel-bridge = { base_url = "x" } }\n'
        restored = self.restore(text)
        self.assertFalse(restored.changed)
        self.assertEqual(restored.stuck, ['model_provider = "excel-bridge"'])
        self.assertEqual(self.path.read_text(), text)

    def test_twice_is_once_and_the_backup_stays(self):
        self.restore('model_provider = "excel-bridge"\n')
        backup = self.path.with_name("config.toml" + desktop_config.RESTORE_BACKUP_SUFFIX)
        once = self.path.read_text()
        again = desktop_config.official_file(self.path)
        self.assertFalse(again.changed)
        self.assertEqual(self.path.read_text(), once)
        self.assertEqual(backup.read_text(), 'model_provider = "excel-bridge"\n')

    def test_nothing_to_do(self):
        self.assertFalse(desktop_config.official_file(self.path).changed)
        self.assertFalse(self.path.exists())
        restored = self.restore(USER_CONFIG.split("[model_providers")[0])
        self.assertEqual((restored.changed, restored.kept, restored.stuck), (False, [], []))

    def test_desktop_later_leaves_what_it_took_out_alone(self):
        self.restore(USER_CONFIG)
        after = self.path.read_text()
        desktop_config.enable_file(self.path, port=8765, catalog=CATALOG, model="gpt-6-sol-excel")
        self.assertTrue(desktop_config.disable_file(self.path))
        self.assertEqual(self.path.read_text(), after)


class RestoreCommandTests(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.config = root / "codex-home" / "config.toml"
        self.config.parent.mkdir()
        env = {"CODEX_HOME": str(self.config.parent), "EXCEL_BRIDGE_HOME": str(root / "bridge-home")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        seen = mock.patch.object(codex_threads, "codex_seen", return_value=False)
        self.codex_seen = seen.start()
        self.addCleanup(seen.stop)

    def run_restore(self) -> tuple[int, list[str]]:
        with mock.patch.object(cli, "_print") as printed:
            code = cli.main(["restore"])
        return code, [call.args[0] for call in printed.call_args_list]

    def test_after_a_window_that_did_not_put_the_config_back(self):
        self.config.write_text(USER_CONFIG)
        desktop_config.enable_file(self.config, port=8765, catalog=CATALOG, model="gpt-6-sol-excel")
        self.codex_seen.return_value = True
        code, said = self.run_restore()
        self.assertEqual(code, 0)
        self.assertEqual(self.config.read_text().replace(desktop_config.RESTORE_PREFIX, ""), USER_CONFIG)
        self.assertEqual(tomllib.loads(self.config.read_text())["model_provider"], "openai")
        self.assertEqual(said[0], f"Codex is back on its own setup: {self.config}")
        self.assertIn("    [model_providers.excel-bridge] (the whole table)", said)
        self.assertIn(f"  The file as it was is saved as config.toml{desktop_config.RESTORE_BACKUP_SUFFIX}.", said)
        self.assertEqual(said[-2:], [cli._REOPEN_AFTER_RESTORE, cli._STILL_RUNNING])

    def test_nothing_to_do(self):
        code, said = self.run_restore()
        self.assertEqual(code, 0)
        self.assertEqual(said, [f"Nothing of the bridge's in {self.config}; Codex is on its own setup there."])
        self.assertFalse(self.config.exists())

    def test_a_relay_is_said(self):
        self.config.write_text('openai_base_url = "https://relay.example/v1"\n')
        code, said = self.run_restore()
        self.assertEqual(code, 0)
        self.assertEqual(said[0], f"Nothing of the bridge's in {self.config}.")
        self.assertEqual(said[-1], '    openai_base_url = "https://relay.example/v1"')

    def test_what_it_cannot_take_out_fails(self):
        self.config.write_text('model_provider = "excel-bridge"\nmodel_providers = { excel-bridge = {} }\n')
        code, said = self.run_restore()
        self.assertEqual(code, 1)
        self.assertEqual(said[0], f"The bridge is still set up in {self.config}:")
        self.assertIn('    model_provider = "excel-bridge"', said)
        self.assertNotIn(cli._REOPEN_AFTER_RESTORE, said)

    def test_signed_in_codex_gets_the_bridge_s_conversations(self):
        (self.config.parent / "auth.json").write_text('{"auth_mode": "apikey", "OPENAI_API_KEY": "sk-test"}')
        with mock.patch.object(cli, "_move_bridge_threads") as move:
            self.run_restore()
        move.assert_called_once_with(self.config.parent)

    def test_without_a_sign_in_it_says_how(self):
        with mock.patch.object(codex_threads, "bridge_threads", return_value=[object(), object()]), \
                mock.patch.object(cli, "_move_bridge_threads", side_effect=AssertionError):
            code, said = self.run_restore()
        self.assertEqual(code, 0)
        self.assertTrue(any(line.startswith("  2 conversation(s) are filed under the bridge's own provider")
                            and "`excel-codex threads migrate`" in line for line in said))

    def test_the_windows_timezone_goes_back(self):
        from excel_codex_bridge import system_timezone

        cases = ((lambda: "China Standard Time", "  Windows timezone: put back China Standard Time."),
                 (lambda: None, None))
        for restore, line in cases:
            with self.subTest(line=line), mock.patch.object(cli.sys, "platform", "win32"), \
                    mock.patch.object(system_timezone, "restore", restore):
                code, said = self.run_restore()
            self.assertEqual(code, 0)
            self.assertEqual([s for s in said if "timezone" in s], [line] if line else [])

        def refused():
            raise system_timezone.Refused("tzutil failed")

        with mock.patch.object(cli.sys, "platform", "win32"), mock.patch.object(system_timezone, "restore", refused):
            code, said = self.run_restore()
        self.assertEqual(code, 0)
        self.assertTrue(any("tzutil failed" in s and "excel-codex timezone restore" in s for s in said))

    def test_what_it_says_is_ascii(self):
        self.config.write_text(
            'model_provider = "excel-bridge"\nopenai_base_url = "http://127.0.0.1:8765/v1"\n'
            'model_providers.excel-bridge.name = "x"\n'
        )
        with mock.patch.object(codex_threads, "bridge_threads", return_value=[object()]):
            _, said = self.run_restore()
        for line in said:
            line.replace(str(self.config), "").encode("ascii")

    def test_double_click_launchers(self):
        repo = Path(__file__).resolve().parents[1]
        command = repo / "excel-codex-restore.command"
        self.assertTrue(os.access(command, os.X_OK) or sys.platform == "win32")
        text = command.read_text(encoding="ascii")
        self.assertIn('exec "$here/excel-codex" restore "$@"', text)
        self.assertIn('exec "$here/excel-codex.sh" restore "$@"', text)
        self.assertNotIn("\r", text)


# Loses track of the first connection the way asyncio's Windows proactor does when a
# client resets it, then runs the CLI.
LEAKY_DESKTOP = """\
import sys
from asyncio import base_events
from excel_codex_bridge import cli

detach = base_events.Server._detach
base_events.Server._detach = lambda self: setattr(base_events.Server, "_detach", detach)
sys.exit(cli.main(sys.argv[1:]))
"""


class ServeCommandTests(unittest.TestCase):
    """`serve`, run by hand next to a config from `print-config`."""

    def setUp(self):
        root = Path(tempfile.mkdtemp())
        self.webview = write_webview_session(root / "webview", time.time() + 3 * 86400)
        self.home = root / "bridge-home"
        env = {"CODEX_HOME": str(root / "codex-home"), "EXCEL_BRIDGE_HOME": str(self.home),
               "EXCEL_BRIDGE_UPDATE_CHECK": "0"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def serve(self, *extra):
        with mock.patch.object(cli, "_run_bridge") as run, mock.patch.object(cli, "_print"):
            code = cli.main(["serve", "--webview-dir", str(self.webview), "--no-auto-signin", *extra])
        run.assert_called_once()
        return code

    def test_an_earlier_release_s_model_list_is_brought_up_to_date(self):
        # What 0.5.1's print-config left: four models, none of them 1M.
        self.home.mkdir()
        catalog = self.home / "codex-model-catalog.json"
        catalog.write_text('{"models": [{"slug": "gpt-5.6-sol-excel"}]}', encoding="utf-8")
        self.assertEqual(self.serve(), 0)
        self.assertEqual(json.loads(catalog.read_text(encoding="utf-8")), codex_config.catalog_payload())

    def test_a_model_list_it_cannot_write_does_not_stop_it(self):
        with mock.patch.object(codex_config, "write_catalog", side_effect=PermissionError("read-only")), \
             self.assertLogs("excel_codex_bridge.cli", "WARNING") as logs:
            self.assertEqual(self.serve(), 0)
        self.assertIn("read-only", logs.output[0])

    def test_the_bridge_excel_codex_starts_leaves_the_list_to_it(self):
        # `excel-codex` writes the list itself before it starts `serve --log-file` in the background.
        log = self.home / "bridge.log"
        self.home.mkdir()
        with mock.patch.object(codex_config, "write_catalog") as write, \
             mock.patch.object(cli.logging, "basicConfig"):
            self.assertEqual(self.serve("--log-file", str(log)), 0)
        write.assert_not_called()


class DesktopShutdownTests(unittest.TestCase):
    def test_ctrl_c_restores_the_config_after_a_lost_connection(self):
        root = Path(tempfile.mkdtemp())
        webview = write_webview_session(root / "webview", time.time() + 3 * 86400)
        config = root / "codex-home" / "config.toml"
        config.parent.mkdir()
        config.write_text(USER_CONFIG)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = dict(
            os.environ,
            CODEX_HOME=str(config.parent),
            EXCEL_BRIDGE_HOME=str(root / "bridge-home"),
            PYTHONPATH=str(Path(cli.__file__).resolve().parents[1]),
        )
        group = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
            else {"start_new_session": True}
        )
        command = [sys.executable, "-c", LEAKY_DESKTOP, "desktop", "--webview-dir", str(webview),
                   "--no-auto-signin", "--port", str(port)]
        desktop = subprocess.Popen(
            command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **group
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            deadline = time.monotonic() + 30
            while desktop.poll() is None and time.monotonic() < deadline:
                try:
                    with opener.open(f"http://127.0.0.1:{port}/healthz", timeout=2):
                        break
                except OSError:
                    time.sleep(0.2)
            self.assertIsNone(desktop.poll(), "the desktop bridge exited early")
            self.assertTrue(desktop_config.is_enabled(config.read_text()))
            if sys.platform == "win32":
                os.kill(desktop.pid, signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(desktop.pid, signal.SIGINT)
            output, _ = desktop.communicate(timeout=30)
        finally:
            if desktop.poll() is None:
                desktop.kill()
                desktop.communicate()
        self.assertEqual(desktop.returncode, 0, output)
        self.assertEqual(config.read_text(), USER_CONFIG)
        # Each request shows up in the window, so users can tell Codex reaches the bridge.
        self.assertIn(b'"GET /healthz HTTP/1.1" 200', output)


if __name__ == "__main__":
    unittest.main()

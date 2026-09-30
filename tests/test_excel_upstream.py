import asyncio
import base64
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

from excel_codex_bridge import excel_upstream


def _jwt_with_exp(expiration: float) -> str:
    def encode(value):
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode({'exp': expiration})}."


class ExcelUpstreamTests(unittest.TestCase):
    def test_session_store_keeps_only_allowlisted_headers(self):
        store = excel_upstream.ExcelSessionStore()
        status = store.configure(
            {
                "Authorization": f"Bearer {_jwt_with_exp(time.time() + 600)}",
                "ChatGPT-Account-ID": "account-1",
                "Cookie": "must-not-be-forwarded",
            }
        )

        self.assertTrue(status["configured"])
        headers = store.request_headers(stream=True)
        self.assertNotIn("cookie", headers)
        self.assertEqual(headers["x-openai-account-id"], "account-1")
        self.assertEqual(headers["accept"], "text/event-stream")
        self.assertEqual(headers["accept-encoding"], "identity")

    def test_session_store_keeps_captured_tools_version(self):
        store = excel_upstream.ExcelSessionStore()
        status = store.configure(
            {
                "authorization": f"Bearer {_jwt_with_exp(time.time() + 600)}",
                "chatgpt-account-id": "account-1",
            },
            tools_version_id="tools-excel-core-2026-06-16-3af59f22",
        )
        self.assertEqual(
            status["tools_version_id"],
            "tools-excel-core-2026-06-16-3af59f22",
        )
        self.assertEqual(
            store.tools_version_id(),
            "tools-excel-core-2026-06-16-3af59f22",
        )
        with self.assertRaisesRegex(ValueError, "not a valid"):
            store.configure(
                {
                    "authorization": f"Bearer {_jwt_with_exp(time.time() + 600)}",
                    "chatgpt-account-id": "account-1",
                },
                tools_version_id="../invalid",
            )

    def test_expired_session_is_rejected(self):
        store = excel_upstream.ExcelSessionStore()
        with self.assertRaisesRegex(ValueError, "already expired"):
            store.configure(
                {
                    "authorization": f"Bearer {_jwt_with_exp(time.time() - 1)}",
                    "chatgpt-account-id": "account-1",
                }
            )

    def test_responses_body_uses_excel_wire_shape(self):
        source = {
            "model": "gpt-5.6-sol-excel",
            "instructions": "Use the client tools.",
            "input": "Hello",
            "prompt_cache_key": "conversation-1",
            "reasoning": {"effort": "xhigh", "summary": "auto"},
            "stream": True,
            "include": ["reasoning.encrypted_content"],
            "text": {"verbosity": "low"},
            "max_output_tokens": 1234,
            "tools": [{"type": "function", "name": "demo"}],
        }
        body = excel_upstream.prepare_responses_body(source)

        self.assertEqual(body["model"], "gpt-5.6-sol")
        self.assertEqual(body["model_selection"], "explicit")
        self.assertFalse(body["store"])
        self.assertEqual(body["reasoning_effort"], "xhigh")
        self.assertEqual(body["prompt_cache_key"], "conversation-1")
        self.assertEqual(body["input"][0]["role"], "developer")
        self.assertEqual(
            body["input"][0]["content"][0]["text"], "Use the client tools."
        )
        self.assertEqual(body["input"][1]["role"], "developer")
        tool_prompt = body["input"][1]["content"][0]["text"]
        self.assertIn("external Codex Responses API client", tool_prompt)
        self.assertIn("native run_officejs", tool_prompt)
        self.assertIn("functions.run_officejs", tool_prompt)
        self.assertIn("inner name is never run_officejs", tool_prompt)
        self.assertIn('\"name\":\"exec_command\"', tool_prompt)
        self.assertIn('"name":"demo"', tool_prompt)
        self.assertEqual(body["input"][3]["role"], "user")
        self.assertNotIn("tools", body)
        self.assertNotIn("include", body)
        self.assertNotIn("text", body)
        self.assertNotIn("max_output_tokens", body)
        self.assertNotIn("reasoning", body)

    def test_supported_reasoning_efforts_and_x_high_alias_are_forwarded(self):
        expected = {
            "low": "low",
            "medium": "medium",
            "high": "high",
            "xhigh": "xhigh",
            "x-high": "xhigh",
            # The backend refuses these two; xhigh is its deepest.
            "max": "xhigh",
            "ultra": "xhigh",
            "ULTRA": "xhigh",
            # Unknown ones stay on the default.
            "persistent": "medium",
        }
        for requested, forwarded in expected.items():
            with self.subTest(requested=requested):
                body = excel_upstream.prepare_responses_body(
                    {
                        "model": "gpt-5.6-sol-excel",
                        "input": "Hello",
                        "reasoning": {"effort": requested},
                    }
                )
                self.assertEqual(body["reasoning_effort"], forwarded)

        for model_id in excel_upstream.MODEL_IDS:
            with self.subTest(model_id=model_id):
                self.assertEqual(
                    excel_upstream.LOCAL_MODEL_CAPABILITIES[model_id]["reasoning_efforts"],
                    ["low", "medium", "high", "xhigh"],
                )

    def test_each_excel_alias_routes_to_matching_upstream_model(self):
        expected = {
            "gpt-5.6-luna-excel": "gpt-5.6-luna",
            "gpt-5.6-terra-excel": "gpt-5.6-terra",
            "gpt-5.6-sol-excel": "gpt-5.6-sol",
            "gpt-6-sol-excel": "gpt-6-sol",
            "gpt-6-luna-excel": "gpt-6-luna",
            "gpt-6-sol-1m-excel": "gpt-6-sol",
            "gpt-6-luna-1m-excel": "gpt-6-luna",
            "gpt-6-astra-1m-excel": "gpt-6-astra",
            "gpt-5.6-sol-1m-excel": "gpt-5.6-sol",
            "gpt-5.6-terra-1m-excel": "gpt-5.6-terra",
            "gpt-5.6-luna-1m-excel": "gpt-5.6-luna",
        }
        for requested, upstream in expected.items():
            with self.subTest(requested=requested):
                body = excel_upstream.prepare_responses_body(
                    {"model": requested, "input": "Hello"}
                )
                self.assertEqual(body["model"], upstream)
                self.assertEqual(body["model_selection"], "explicit")
                self.assertTrue(excel_upstream.is_excel_model(requested))
        self.assertFalse(excel_upstream.is_excel_model("gpt-excel"))

    def test_default_backend_compaction_follows_the_alias_window(self):
        for requested, threshold in (
            ("gpt-6-sol-excel", 475_000),
            ("gpt-5.6-luna-excel", 475_000),
            ("gpt-6-sol-1m-excel", 872_000),
            ("gpt-5.6-luna-1m-excel", 872_000),
        ):
            with self.subTest(requested=requested):
                body = excel_upstream.prepare_responses_body({"model": requested, "input": "Hello"})
                self.assertEqual(
                    body["context_management"], [{"type": "compaction", "compact_threshold": threshold}]
                )
        explicit = [{"type": "compaction", "compact_threshold": 50_000}]
        body = excel_upstream.prepare_responses_body(
            {"model": "gpt-6-sol-1m-excel", "input": "Hello", "context_management": explicit}
        )
        self.assertEqual(body["context_management"], explicit)

    def test_task_identity_is_stable_for_a_conversation(self):
        source = {
            "model": "gpt-5.6-sol-excel",
            "input": "Hello",
            "prompt_cache_key": "conversation-1",
        }
        first = excel_upstream.prepare_responses_body(source)
        second = excel_upstream.prepare_responses_body(source)
        self.assertEqual(
            first["metadata"]["task_id"], second["metadata"]["task_id"]
        )
        other = excel_upstream.prepare_responses_body(
            {**source, "prompt_cache_key": "conversation-2"}
        )
        self.assertNotEqual(
            first["metadata"]["task_id"], other["metadata"]["task_id"]
        )

    def test_identical_requests_serialize_to_identical_bytes(self):
        source = {
            "model": "gpt-5.6-sol-excel",
            "instructions": "Be terse.",
            "prompt_cache_key": "conversation-1",
            "tools": [{"type": "function", "name": "demo"}],
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                }
            ],
        }
        first = json.dumps(excel_upstream.prepare_responses_body(source), sort_keys=True)
        second = json.dumps(excel_upstream.prepare_responses_body(source), sort_keys=True)
        # A retry of the same turn must not look like new work to the upstream.
        self.assertEqual(first, second)

    def test_turn_identity_is_derived_without_a_cache_key(self):
        base = {
            "model": "gpt-5.6-sol-excel",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                }
            ],
        }
        first = excel_upstream.prepare_responses_body(base)
        self.assertEqual(
            first["metadata"],
            excel_upstream.prepare_responses_body(base)["metadata"],
        )
        # A different conversation must not collapse onto the same identity.
        other = excel_upstream.prepare_responses_body(
            {
                **base,
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "different"}],
                    }
                ],
            }
        )
        self.assertNotEqual(first["metadata"]["task_id"], other["metadata"]["task_id"])
        # Tool iterations stay inside the same Excel turn and increment only
        # agent_iteration.
        tool_history = base["input"] + [
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "done",
            }
        ]
        later = excel_upstream.prepare_responses_body(
            {**base, "input": tool_history}
        )
        self.assertEqual(first["metadata"]["task_id"], later["metadata"]["task_id"])
        self.assertEqual(first["metadata"]["turn_id"], later["metadata"]["turn_id"])
        self.assertEqual(later["metadata"]["agent_iteration"], "2")

        next_turn = excel_upstream.prepare_responses_body(
            {
                **base,
                "input": tool_history
                + [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "continue"}],
                    }
                ],
            }
        )
        self.assertEqual(first["metadata"]["task_id"], next_turn["metadata"]["task_id"])
        self.assertNotEqual(first["metadata"]["turn_id"], next_turn["metadata"]["turn_id"])
        self.assertEqual(next_turn["metadata"]["agent_iteration"], "1")

    def test_parallel_results_count_as_one_iteration(self):
        user = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]}

        def call(n):
            return {"type": "function_call", "call_id": f"call_{n}", "name": "exec_command", "arguments": "{}"}

        def result(n):
            return {"type": "function_call_output", "call_id": f"call_{n}", "output": "ok"}

        def iteration(items):
            body = excel_upstream.prepare_responses_body({"model": "gpt-5.6-sol-excel", "input": [user, *items]})
            return body["metadata"]["agent_iteration"]

        self.assertEqual(iteration([call(1), call(2), result(1), result(2)]), "2")
        self.assertEqual(iteration([call(1), result(1), call(2), result(2)]), "3")
        self.assertEqual(iteration([call(1), call(2), result(1), result(2), call(3), result(3)]), "3")

    def test_turn_identity_ignores_how_pictures_were_passed_on(self):
        def body(url: str) -> dict:
            return {
                "model": "gpt-5.6-sol-excel",
                "input": [
                    {"type": "message", "role": "user", "content": [
                        {"type": "input_image", "image_url": url, "detail": "high"},
                        {"type": "input_text", "text": "what is this?"},
                    ]},
                    {"type": "function_call_output", "call_id": "call_1", "output": "done"},
                ],
            }

        original = body("data:image/png;base64,iVBORw0KGgo=")["input"]
        # Inline on one attempt, uploaded or a text note on the next.
        first = excel_upstream.prepare_responses_body(
            body("https://a.example.com/i/x.png"), identity_input=original
        )
        second = excel_upstream.prepare_responses_body(
            body("https://b.example.com/i/x.png"), identity_input=original
        )
        self.assertEqual(first["metadata"]["turn_id"], second["metadata"]["turn_id"])
        self.assertEqual(first["metadata"]["task_id"], second["metadata"]["task_id"])
        self.assertEqual(second["metadata"]["agent_iteration"], "2")
        self.assertIn("b.example.com", json.dumps(second["input"]))
        unpinned = excel_upstream.prepare_responses_body(body("https://b.example.com/i/x.png"))
        self.assertNotEqual(first["metadata"]["turn_id"], unpinned["metadata"]["turn_id"])

    def test_encrypted_reasoning_is_replayed_and_bare_reasoning_dropped(self):
        items = excel_upstream.translate_input_items(
            [
                {"type": "reasoning", "summary": [], "encrypted_content": "gAAA=="},
                {"type": "reasoning", "id": "rs_bare", "summary": []},
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "done"}],
                },
            ]
        )
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["type"], "reasoning")
        self.assertEqual(items[0]["encrypted_content"], "gAAA==")
        self.assertEqual(items[1]["type"], "message")

    def test_tool_history_is_replayed_with_relay_namespace(self):
        raw_input = [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "list files"}],
            },
            {"type": "reasoning", "id": "rs_1", "summary": []},
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_ghcp_excel_marker_1",
                "name": "shell_command",
                "arguments": '{"command":"ls"}',
            },
            {
                "type": "function_call_output",
                "id": "fc_call_ghcp_excel_marker_1",
                "call_id": "call_ghcp_excel_marker_1",
                "output": "file.txt",
            },
        ]
        items = excel_upstream.translate_input_items(
            raw_input, {"shell_command": "function"}
        )

        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["role"], "user")
        # Standard item shapes are retained, but the upstream-visible tool name
        # cannot collide with a server-injected Basispoints tool.
        self.assertEqual(items[1]["type"], "function_call")
        self.assertEqual(items[1]["name"], "codex_client__shell_command")
        self.assertEqual(items[1]["call_id"], "call_ghcp_excel_marker_1")
        self.assertIs(items[2], raw_input[3])
        # Deterministic rendering keeps the upstream prompt-cache prefix stable.
        self.assertEqual(
            items,
            excel_upstream.translate_input_items(
                raw_input, {"shell_command": "function"}
            ),
        )

    def test_plan_history_is_namespaced_without_rewriting_output(self):
        items = excel_upstream.translate_input_items(
            [
                {
                    "type": "function_call",
                    "call_id": "call_ghcp_excel_marker_1",
                    "name": "update_plan",
                    "arguments": "{}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_ghcp_excel_marker_1",
                    "output": "Plan updated",
                },
            ],
            {"update_plan": "function", "shell_command": "function"},
        )
        self.assertEqual(items[0]["name"], "codex_client__update_plan")
        self.assertEqual(items[1]["output"], "Plan updated")

    def test_native_plan_history_preserves_identity_and_executor_result(self):
        raw_call = {
            "type": "function_call",
            "id": "fc_native",
            "call_id": "call_native",
            "name": "update_plan",
            "arguments": (
                '{"explanation":"Inspect repository","plan":['
                '{"step":"Read code","status":"in_progress"}]}'
            ),
        }
        raw_output = {
            "type": "function_call_output",
            "call_id": "call_native",
            "output": "Plan updated",
        }
        items = excel_upstream.translate_input_items(
            [raw_call, raw_output],
            {"update_plan": "function"},
        )
        self.assertEqual(items[0]["call_id"], "call_native")
        self.assertEqual(items[0]["name"], "update_plan")
        self.assertEqual(
            json.loads(items[0]["arguments"]),
            {
                "summary": "Inspect repository",
                "plan": [
                    {
                        "id": "step1",
                        "description": "Read code",
                        "status": "in_progress",
                        "result": "",
                    }
                ],
            },
        )
        self.assertEqual(items[1]["call_id"], "call_native")
        self.assertEqual(items[1]["output"], '{"status":"ok"}')

    def test_run_officejs_transport_round_trips_original_native_item(self):
        source = {
            "tools": [
                {
                    "type": "function",
                    "name": "shell_command",
                    "parameters": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                        "required": ["command"],
                    },
                }
            ]
        }
        native = {
            "type": "function_call",
            "id": "fc_transport_round_trip",
            "call_id": "call_transport_round_trip",
            "name": "run_officejs",
            "status": "completed",
            "arguments": json.dumps(
                {
                    "summary": "Inspect repository",
                    "extended_summary": "List repository files",
                    "code": "const request = "
                    + json.dumps(
                        {
                            "name": "shell_command",
                            "arguments": {"command": "Get-ChildItem"},
                        },
                        separators=(",", ":"),
                    )
                    + ";",
                    "destructive": False,
                    "references": [],
                },
                separators=(",", ":"),
            ),
            "internal_chat_message_metadata_passthrough": {
                "turn_id": "native-turn"
            },
        }
        tool_call = excel_upstream.extract_native_client_tool_call(
            {
                "output": [
                    {"type": "message", "phase": "commentary"},
                    native,
                ]
            },
            source,
        )
        self.assertEqual(tool_call["name"], "shell_command")
        self.assertEqual(tool_call["call_id"], native["call_id"])
        self.assertEqual(
            json.loads(tool_call["arguments"]),
            {"command": "Get-ChildItem"},
        )

        replay = excel_upstream.translate_input_items(
            [
                tool_call,
                {
                    "type": "function_call_output",
                    "call_id": native["call_id"],
                    "output": "file.txt",
                },
            ],
            {"shell_command": "function"},
        )
        self.assertEqual(replay[0], native)
        self.assertEqual(replay[1]["output"], "file.txt")

    def test_run_officejs_transport_unwraps_single_nested_envelope(self):
        source = {
            "tools": [
                {
                    "type": "function",
                    "name": "exec_command",
                    "parameters": {
                        "type": "object",
                        "properties": {"cmd": {"type": "string"}},
                        "required": ["cmd"],
                        "additionalProperties": False,
                    },
                }
            ]
        }
        inner = {"name": "exec_command", "arguments": {"cmd": "pwd"}}
        wrapper = {
            "name": "run_officejs",
            "arguments": {"code": json.dumps(inner, separators=(",", ":"))},
        }
        native = {
            "type": "function_call",
            "call_id": "call_single_nested_transport",
            "name": "run_officejs",
            "arguments": json.dumps(
                {"code": json.dumps(wrapper, separators=(",", ":"))}
            ),
        }

        tool_call = excel_upstream.extract_native_client_tool_call(
            {"output": [native]}, source
        )

        self.assertEqual(tool_call["name"], "exec_command")
        self.assertEqual(json.loads(tool_call["arguments"]), {"cmd": "pwd"})
        replay = excel_upstream.translate_input_items(
            [
                tool_call,
                {
                    "type": "function_call_output",
                    "call_id": native["call_id"],
                    "output": "ok",
                },
            ],
            {"exec_command": "function"},
        )
        self.assertEqual(replay[0], native)
        self.assertEqual(replay[1]["output"], "ok")

    @staticmethod
    def _exec_command_source() -> dict:
        return {
            "tools": [
                {
                    "type": "function",
                    "name": "exec_command",
                    "parameters": {
                        "type": "object",
                        "properties": {"cmd": {"type": "string"}},
                        "required": ["cmd"],
                    },
                }
            ]
        }

    @staticmethod
    def _transport_call(call_id: str, code: object) -> dict:
        return {
            "type": "function_call",
            "id": f"fc_{call_id}",
            "call_id": call_id,
            "name": "run_officejs",
            "arguments": json.dumps(
                {"code": code if isinstance(code, str) else json.dumps(code)}
            ),
        }

    def test_parallel_calls_are_all_converted_and_replayed(self):
        first = self._transport_call(
            "call_first", {"name": "exec_command", "arguments": {"cmd": "pwd"}}
        )
        second = self._transport_call(
            "call_second", {"name": "exec_command", "arguments": {"cmd": "ls"}}
        )
        response = {"output": [{"type": "reasoning", "id": "rs_1"}, first, second]}
        source = {**self._exec_command_source(), "parallel_tool_calls": True}

        calls = excel_upstream.extract_native_client_tool_calls(response, source)

        self.assertEqual([call["call_id"] for call in calls], ["call_first", "call_second"])
        self.assertEqual([json.loads(call["arguments"]) for call in calls], [{"cmd": "pwd"}, {"cmd": "ls"}])
        payload = excel_upstream.response_payload_with_tool_calls(response, calls)
        self.assertEqual(
            [(item["type"], item.get("call_id")) for item in payload["output"]],
            [("reasoning", None), ("function_call", "call_first"), ("function_call", "call_second")],
        )
        self.assertNotIn("run_officejs", json.dumps(payload))
        # Codex sends both calls back, then both results.
        replay = excel_upstream.translate_input_items(
            [
                *calls,
                {"type": "function_call_output", "call_id": "call_first", "output": "/w"},
                {"type": "function_call_output", "call_id": "call_second", "output": "a b"},
            ],
            {"exec_command": "function"},
        )
        self.assertEqual(replay[:2], [first, second])
        self.assertEqual([item["call_id"] for item in replay[2:]], ["call_first", "call_second"])

    def test_parallel_calls_turned_off_run_the_first(self):
        calls = [
            self._transport_call(f"call_{n}", {"name": "exec_command", "arguments": {"cmd": cmd}})
            for n, cmd in enumerate(("pwd", "ls"))
        ]
        source = {**self._exec_command_source(), "parallel_tool_calls": False}

        converted = excel_upstream.extract_native_client_tool_calls({"output": calls}, source)

        self.assertEqual([call["call_id"] for call in converted], ["call_0"])
        payload = excel_upstream.response_payload_with_tool_calls({"output": calls}, converted)
        self.assertEqual([item["call_id"] for item in payload["output"]], ["call_0"])

    def test_unusable_call_among_parallel_calls_is_left_out(self):
        broken = self._transport_call("call_broken", "not json")
        usable = self._transport_call(
            "call_usable", {"name": "exec_command", "arguments": {"cmd": "ls"}}
        )

        calls = excel_upstream.extract_native_client_tool_calls(
            {"output": [broken, usable]}, self._exec_command_source()
        )

        self.assertEqual([call["call_id"] for call in calls], ["call_usable"])
        self.assertEqual(json.loads(calls[0]["arguments"]), {"cmd": "ls"})

    def test_prompt_invites_parallel_calls_unless_turned_off(self):
        source = self._exec_command_source()
        allowed = excel_upstream._client_tool_protocol_instructions(source)
        self.assertIn("separate run_officejs calls in the same response", allowed)
        self.assertNotIn("call the outer native run_officejs tool once", allowed)
        serial = excel_upstream._client_tool_protocol_instructions({**source, "parallel_tool_calls": False})
        self.assertIn("call the outer native run_officejs tool once", serial)

    def test_run_officejs_transport_repairs_invalid_shell_backslashes(self):
        source = {
            "tools": [
                {
                    "type": "function",
                    "name": "exec_command",
                    "parameters": {
                        "type": "object",
                        "properties": {"cmd": {"type": "string"}},
                        "required": ["cmd"],
                    },
                }
            ]
        }
        malformed_inner = r'{"name":"exec_command","arguments":{"cmd":"rg -n \( pattern"}}'
        native = {
            "type": "function_call",
            "call_id": "call_repair_invalid_backslash",
            "name": "run_officejs",
            "arguments": json.dumps({"code": malformed_inner}),
        }

        tool_call = excel_upstream.extract_native_client_tool_call(
            {"output": [native]}, source
        )

        self.assertEqual(tool_call["name"], "exec_command")
        self.assertEqual(
            json.loads(tool_call["arguments"]),
            {"cmd": r"rg -n \( pattern"},
        )

    def test_namespaced_run_officejs_transport_alias_is_accepted(self):
        source = {
            "tools": [
                {
                    "type": "function",
                    "name": "exec_command",
                    "parameters": {
                        "type": "object",
                        "properties": {"cmd": {"type": "string"}},
                        "required": ["cmd"],
                        "additionalProperties": False,
                    },
                }
            ]
        }
        native = {
            "type": "function_call",
            "call_id": "call_namespaced_transport",
            "name": "functions.run_officejs",
            "arguments": json.dumps(
                {
                    "code": json.dumps(
                        {"name": "exec_command", "arguments": {"cmd": "pwd"}},
                        separators=(",", ":"),
                    )
                }
            ),
        }

        tool_call = excel_upstream.extract_native_client_tool_call(
            {"output": [native]}, source
        )

        self.assertEqual(tool_call["name"], "exec_command")
        self.assertEqual(json.loads(tool_call["arguments"]), {"cmd": "pwd"})

    def test_run_officejs_transport_unwraps_double_nested_envelope(self):
        source = {
            "tools": [
                {
                    "type": "function",
                    "name": "exec_command",
                    "parameters": {
                        "type": "object",
                        "properties": {"cmd": {"type": "string"}},
                        "required": ["cmd"],
                        "additionalProperties": False,
                    },
                }
            ]
        }
        envelope = {"name": "exec_command", "arguments": {"cmd": "pwd"}}
        for _ in range(2):
            envelope = {
                "name": "run_officejs",
                "arguments": {
                    "code": json.dumps(envelope, separators=(",", ":"))
                },
            }
        native = {
            "type": "function_call",
            "call_id": "call_double_nested_transport",
            "name": "run_officejs",
            "arguments": json.dumps(
                {"code": json.dumps(envelope, separators=(",", ":"))}
            ),
        }

        tool_call = excel_upstream.extract_native_client_tool_call(
            {"output": [native]}, source
        )

        self.assertEqual(tool_call["name"], "exec_command")
        self.assertEqual(json.loads(tool_call["arguments"]), {"cmd": "pwd"})

    def test_run_officejs_transport_rejects_deeper_recursive_envelope(self):
        source = {
            "tools": [
                {
                    "type": "function",
                    "name": "exec_command",
                    "parameters": {"type": "object"},
                },
                {
                    "type": "function",
                    "name": "run_officejs",
                    "parameters": {"type": "object"},
                },
            ]
        }
        envelope = {"name": "exec_command", "arguments": {"cmd": "pwd"}}
        for _ in range(3):
            envelope = {
                "name": "run_officejs",
                "arguments": {
                    "code": json.dumps(envelope, separators=(",", ":"))
                },
            }
        native = {
            "type": "function_call",
            "call_id": "call_recursive_transport",
            "name": "run_officejs",
            "arguments": json.dumps(
                {"code": json.dumps(envelope, separators=(",", ":"))}
            ),
        }

        self.assertIsNone(
            excel_upstream.extract_native_client_tool_call(
                {"output": [native]}, source
            )
        )

    def test_run_officejs_transport_validates_unwrapped_arguments(self):
        source = {
            "tools": [
                {
                    "type": "function",
                    "name": "exec_command",
                    "parameters": {
                        "type": "object",
                        "properties": {"cmd": {"type": "string"}},
                        "required": ["cmd"],
                        "additionalProperties": False,
                    },
                }
            ]
        }
        inner = {"name": "exec_command", "arguments": {"cmd": 123}}
        wrapper = {
            "name": "run_officejs",
            "arguments": {"code": json.dumps(inner, separators=(",", ":"))},
        }
        native = {
            "type": "function_call",
            "call_id": "call_invalid_nested_transport",
            "name": "run_officejs",
            "arguments": json.dumps(
                {"code": json.dumps(wrapper, separators=(",", ":"))}
            ),
        }

        self.assertIsNone(
            excel_upstream.extract_native_client_tool_call(
                {"output": [native]}, source
            )
        )

    def test_custom_transport_result_returns_as_native_function_output(self):
        source = {
            "tools": [
                {
                    "type": "custom",
                    "name": "apply_patch",
                }
            ]
        }
        native = {
            "type": "function_call",
            "id": "fc_custom_transport",
            "call_id": "call_custom_transport",
            "name": "run_officejs",
            "arguments": json.dumps(
                {
                    "summary": "Apply patch",
                    "extended_summary": "Edit a source file",
                    "code": json.dumps(
                        {
                            "name": "apply_patch",
                            "input": "*** Begin Patch\n*** End Patch\n",
                        }
                    ),
                    "destructive": False,
                    "references": [],
                }
            ),
        }
        tool_call = excel_upstream.extract_native_client_tool_call(
            {"output": [native]},
            source,
        )
        self.assertEqual(tool_call["type"], "custom_tool_call")
        replay = excel_upstream.translate_input_items(
            [
                tool_call,
                {
                    "type": "custom_tool_call_output",
                    "id": "ctco_custom_transport",
                    "call_id": native["call_id"],
                    "output": "Done!",
                },
            ],
            {"apply_patch": "custom"},
        )
        self.assertEqual(replay[0], native)
        self.assertEqual(replay[1]["type"], "function_call_output")
        self.assertEqual(replay[1]["id"], "fc_call_custom_transport")
        self.assertEqual(replay[1]["output"], "Done!")

    def test_function_transport_result_repairs_incompatible_output_id(self):
        replay = excel_upstream.translate_input_items(
            [
                {
                    "type": "function_call",
                    "id": "fc_client_exec",
                    "call_id": "call_client_exec",
                    "name": "exec_command",
                    "arguments": '{"cmd":"pwd"}',
                },
                {
                    "type": "function_call_output",
                    "id": "ctco_wrong_prefix",
                    "call_id": "call_client_exec",
                    "output": "ok",
                },
            ],
            {"exec_command": "function"},
        )

        self.assertEqual(replay[0]["type"], "function_call")
        self.assertEqual(replay[0]["name"], "run_officejs")
        self.assertEqual(replay[0]["id"], "fc_call_client_exec")
        self.assertEqual(replay[1]["type"], "function_call_output")
        self.assertEqual(replay[1]["id"], "fc_call_client_exec")
        self.assertEqual(replay[1]["output"], "ok")

    def test_prepare_body_repairs_custom_output_id_for_upstream(self):
        call_id = "call_custom_prepare"
        native = {
            "type": "function_call",
            "id": "fc_custom_prepare",
            "call_id": call_id,
            "name": "run_officejs",
            "arguments": "{}",
            "status": "completed",
        }
        excel_upstream._remember_native_call(native)

        body = excel_upstream.prepare_responses_body(
            {
                "model": "gpt-5.6-sol-excel",
                "input": [
                    {
                        "type": "custom_tool_call",
                        "id": "ctc_custom_prepare",
                        "call_id": call_id,
                        "name": "apply_patch",
                        "input": "patch",
                        "status": "completed",
                    },
                    {
                        "type": "custom_tool_call_output",
                        "id": "ctco_custom_prepare",
                        "call_id": call_id,
                        "output": "Done!",
                    },
                ],
                "tools": [{"type": "custom", "name": "apply_patch"}],
            }
        )

        replay = [
            item
            for item in body["input"]
            if item.get("call_id") == call_id
        ]
        self.assertEqual(replay[0], native)
        self.assertEqual(replay[1]["type"], "function_call_output")
        self.assertEqual(replay[1]["id"], f"fc_{call_id}")
        self.assertEqual(replay[1]["output"], "Done!")

    def test_native_calls_replay_exactly_after_a_restart(self):
        call_id = "call_after_restart"
        native = {
            "type": "function_call",
            "id": "fc_upstream_own_id",
            "call_id": call_id,
            "name": "run_officejs",
            "arguments": '{"code": "{\\"tool\\": \\"exec_command\\"}", "summary": "Look at the sheet"}',
            "status": "completed",
        }
        body = {
            "model": "gpt-5.6-sol-excel",
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
                {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": "exec_command",
                    "arguments": '{"cmd": "ls"}',
                },
                {"type": "function_call_output", "call_id": call_id, "output": "ok"},
            ],
            "tools": [{"type": "function", "name": "exec_command"}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            excel_upstream.keep_native_calls_in(Path(tmp) / "tool-calls.sqlite3")
            try:
                excel_upstream._remember_native_call(native)
                # A new bridge process starts with an empty cache.
                with excel_upstream._native_call_cache_lock:
                    excel_upstream._native_call_cache.clear()
                excel_upstream.keep_native_calls_in(Path(tmp) / "tool-calls.sqlite3")
                replay = [
                    item
                    for item in excel_upstream.prepare_responses_body(body)["input"]
                    if item.get("call_id") == call_id
                ]
            finally:
                excel_upstream.keep_native_calls_in(None)
        self.assertEqual(replay[0], native)
        self.assertEqual(replay[1]["type"], "function_call_output")

    def test_a_broken_call_store_is_only_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tool-calls.sqlite3"
            path.write_bytes(b"not a database" * 100)
            excel_upstream.keep_native_calls_in(path)
            try:
                excel_upstream._remember_native_call(
                    {"type": "function_call", "id": "fc_x", "call_id": "call_broken_store", "name": "run_officejs",
                     "arguments": "{}"}
                )
                self.assertEqual(excel_upstream._remembered_native_call("call_broken_store")["id"], "fc_x")
            finally:
                excel_upstream.keep_native_calls_in(None)

    def test_tools_version_is_forwarded_as_authoritative_metadata(self):
        body = excel_upstream.prepare_responses_body(
            {
                "model": "gpt-5.6-sol-excel",
                "input": "Hello",
                "metadata": {
                    "bps_tools_version_id": "caller-must-not-override",
                },
            },
            tools_version_id="tools-excel-core-2026-06-16-3af59f22",
        )
        self.assertEqual(
            body["metadata"]["bps_tools_version_id"],
            "tools-excel-core-2026-06-16-3af59f22",
        )

    def test_catalog_and_reminder_lead_the_cached_prefix(self):
        body = excel_upstream.prepare_responses_body(
            {
                "model": "gpt-5.6-sol-excel",
                "input": "Hello",
                "tools": [
                    {
                        "type": "function",
                        "name": "demo",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
            }
        )
        catalog = body["input"][0]["content"][0]["text"]
        self.assertIn('"name":"demo"', catalog)
        reminder_item = body["input"][1]
        self.assertEqual(reminder_item["role"], "developer")
        reminder = reminder_item["content"][0]["text"]
        self.assertIn("run_officejs", reminder)
        self.assertIn("functions.run_officejs", reminder)
        self.assertIn("Never set the inner name", reminder)
        # The catalog right above names the tools; the reminder does not repeat them.
        self.assertNotIn("demo", reminder)
        self.assertEqual(body["input"][-1]["role"], "user")
        # Keep the compact cue small relative to the full catalog.
        self.assertLess(len(reminder), len(catalog) / 2)

    def test_protocol_reminder_clarifies_custom_transport_input(self):
        reminder = excel_upstream._client_tool_protocol_reminder(
            {
                "tools": [
                    {"type": "function", "name": "exec_command"},
                    {"type": "custom", "name": "apply_patch"},
                ]
            }
        )

        self.assertIn("transport exec_command", reminder)
        self.assertIn("Custom tools use input, not arguments", reminder)
        self.assertIn("never use arguments.patch", reminder)
        self.assertIn("code field is not JavaScript", reminder)
        self.assertIn("escape backslashes and quotes", reminder)

    def test_unsupported_transport_output_becomes_actionable_guidance(self):
        replay = excel_upstream.translate_input_items(
            [
                {
                    "type": "function_call",
                    "id": "fc_bad_transport",
                    "call_id": "call_bad_transport",
                    "name": "run_officejs",
                    "arguments": "{}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_bad_transport",
                    "output": "unsupported call: run_officejs",
                },
            ],
            {"exec_command": "function"},
        )

        self.assertIn("its code field did not hold a JSON object", replay[1]["output"])
        self.assertNotIn("{reason}", replay[1]["output"])
        self.assertNotEqual(replay[1]["output"], "unsupported call: run_officejs")

    COLLABORATION = {
        "collaboration.spawn_agent": "function",
        "exec_command": "function",
        "apply_patch": "custom",
    }

    def _leaked_replay(self, code: object, call_id: str) -> list:
        return excel_upstream.translate_input_items(
            [
                self._transport_call(call_id, code),
                {"type": "function_call_output", "call_id": call_id,
                 "output": "unsupported call: run_officejs"},
            ],
            self.COLLABORATION,
        )

    def test_leaked_relay_call_is_replayed_as_it_was_not_wrapped_again(self):
        code = {"name": "spawn_agent", "arguments": {"message": "hi", "task_name": "helper"}}
        leaked = self._transport_call("call_leaked_spawn", code)
        replay = self._leaked_replay(code, "call_leaked_spawn")

        self.assertEqual(replay[0]["name"], "run_officejs")
        self.assertEqual(replay[0]["arguments"], leaked["arguments"])
        self.assertEqual(replay[0]["id"], "fc_call_leaked_spawn")
        self.assertEqual(json.loads(json.loads(replay[0]["arguments"])["code"])["name"], "spawn_agent")
        self.assertIn(
            "spawn_agent is not a tool in the catalog; the catalog calls it collaboration.spawn_agent",
            replay[1]["output"],
        )

    def test_rejected_relay_calls_say_why(self):
        def nested(inner: object, times: int) -> dict:
            for _ in range(times):
                inner = {"name": "run_officejs", "arguments": {"code": json.dumps(inner)}}
            return inner

        cases = {
            "its code field did not hold a JSON object": "console.log(1)",
            "it wrapped run_officejs inside run_officejs more than twice": nested(
                {"name": "exec_command", "arguments": {"cmd": "pwd"}}, 3
            ),
            "the object in its code field named no tool": {"arguments": {"cmd": "pwd"}},
            "shell is not a tool in the catalog": {"name": "shell", "arguments": {}},
            "apply_patch is a custom tool: its text goes in input": {
                "name": "apply_patch", "arguments": {"patch": "x"}
            },
            "the arguments for exec_command were not a JSON object": {
                "name": "exec_command", "arguments": "pwd"
            },
        }
        for index, (reason, code) in enumerate(cases.items()):
            with self.subTest(reason=reason):
                replay = self._leaked_replay(code, f"call_reason_{index}")
                self.assertEqual(len(replay), 2)
                self.assertIn(reason, replay[1]["output"])

        bare = excel_upstream.translate_input_items(
            [
                {"type": "function_call", "call_id": "call_reason_bare",
                 "name": "run_officejs", "arguments": "[1]"},
                {"type": "function_call_output", "call_id": "call_reason_bare",
                 "output": "unsupported call: run_officejs"},
            ],
            self.COLLABORATION,
        )
        self.assertIn("its arguments were not a JSON object", bare[1]["output"])

    def test_converted_calls_say_their_arguments_are_plain(self):
        source = {
            "model": "gpt-5.6-sol-excel",
            "input": "Spawn a helper.",
            "tools": [
                {
                    "type": "namespace",
                    "name": "collaboration",
                    "tools": [
                        {
                            "type": "function",
                            "name": "spawn_agent",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "message": {"type": "string"},
                                    "task_name": {"type": "string"},
                                },
                                "required": ["message", "task_name"],
                            },
                        }
                    ],
                }
            ],
        }
        call = excel_upstream.extract_native_client_tool_call(
            {
                "output": [
                    self._transport_call(
                        "call_plain_spawn",
                        {
                            "name": "collaboration.spawn_agent",
                            "arguments": {"message": "hi", "task_name": "helper"},
                        },
                    )
                ]
            },
            source,
        )

        self.assertEqual(call["name"], "spawn_agent")
        self.assertEqual(call["namespace"], "collaboration")
        self.assertEqual(call["encrypted_function_args"], [])

        replay = excel_upstream.translate_input_items(
            [
                {**call, "call_id": "call_plain_spawn_replayed"},
                {"type": "function_call_output", "call_id": "call_plain_spawn_replayed",
                 "output": "spawned"},
            ],
            {"collaboration.spawn_agent": "function"},
        )
        self.assertNotIn("encrypted_function_args", json.dumps(replay))
        self.assertEqual(replay[0]["name"], "run_officejs")
        envelope = json.loads(json.loads(replay[0]["arguments"])["code"])
        self.assertEqual(envelope["name"], "collaboration.spawn_agent")
        self.assertEqual(envelope["arguments"], {"message": "hi", "task_name": "helper"})

    def test_messages_to_other_agents_are_replayed_readable(self):
        sealed = "gAAAAA" + "x" * 60 + "=="
        replay = excel_upstream.translate_input_items(
            [
                {
                    "type": "agent_message",
                    "author": "/root",
                    "recipient": "/root/helper",
                    "content": [
                        {"type": "input_text", "text": "Message Type: NEW_TASK\nPayload:\n"},
                        {"type": "encrypted_content", "encrypted_content": "Count the rows."},
                        {"type": "encrypted_content", "encrypted_content": sealed},
                    ],
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_readable_output",
                    "output": [{"type": "encrypted_content", "encrypted_content": "3 rows"}],
                },
            ]
        )

        self.assertEqual(
            replay[0]["content"][1], {"type": "input_text", "text": "Count the rows."}
        )
        self.assertEqual(
            replay[0]["content"][2], {"type": "encrypted_content", "encrypted_content": sealed}
        )
        self.assertEqual(replay[1]["output"], [{"type": "input_text", "text": "3 rows"}])

    def test_without_sealed_content(self):
        sealed = "gAAAAA" + "y" * 60
        body = {
            "input": [
                {"type": "reasoning", "summary": [], "encrypted_content": sealed},
                {"type": "agent_message", "content": [
                    {"type": "input_text", "text": "Payload:"},
                    {"type": "encrypted_content", "encrypted_content": sealed},
                ]},
                {"type": "function_call_output", "call_id": "c", "output": [
                    {"type": "encrypted_content", "encrypted_content": sealed},
                ]},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            ]
        }

        kept = excel_upstream.without_sealed_content(body)

        self.assertEqual([item["type"] for item in kept["input"]],
                         ["agent_message", "function_call_output", "message"])
        self.assertNotIn(sealed, json.dumps(kept))
        self.assertEqual(kept["input"][0]["content"][0], {"type": "input_text", "text": "Payload:"})
        self.assertIn("another backend", kept["input"][1]["output"][0]["text"])
        plain = {"input": [body["input"][3]]}
        self.assertIs(excel_upstream.without_sealed_content(plain), plain)

    def test_rebuilt_relay_call_keeps_the_namespace(self):
        replay = excel_upstream.translate_input_items(
            [
                {"type": "function_call", "call_id": "call_rebuilt_spawn",
                 "namespace": "collaboration", "name": "spawn_agent",
                 "arguments": json.dumps({"message": "hi", "task_name": "helper"})},
            ],
            {"collaboration.spawn_agent": "function"},
        )

        envelope = json.loads(json.loads(replay[0]["arguments"])["code"])
        self.assertEqual(envelope["name"], "collaboration.spawn_agent")
        self.assertNotIn("namespace", replay[0])

    def test_nested_plugin_tools_are_forwarded_in_catalog(self):
        source = {
            "model": "gpt-5.6-sol-excel",
            "input": "Open Calendar.",
            "tools": [
                {
                    "type": "namespace",
                    "name": "computer_use",
                    "tools": [
                        {
                            "type": "function",
                            "name": "js",
                            "description": "Control desktop applications.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"code": {"type": "string"}},
                                "required": ["code"],
                            },
                        }
                    ],
                }
            ],
        }

        self.assertEqual(
            excel_upstream.client_tool_types(source),
            {"computer_use.js": "function"},
        )
        body = excel_upstream.prepare_responses_body(source)
        catalog = body["input"][0]["content"][0]["text"]
        self.assertIn('"name":"computer_use.js"', catalog)
        # A plugin's namespace is summarized: its first sentence and each parameter's type.
        self.assertIn('"summary":"Control desktop applications."', catalog)
        self.assertIn('"parameters":{"code":"string, required"}', catalog)

        tool_call = excel_upstream.extract_native_client_tool_call(
            {
                "output": [
                    {
                        "type": "function_call",
                        "id": "fc_transport",
                        "call_id": "call_transport",
                        "name": "run_officejs",
                        "arguments": json.dumps(
                            {
                                "code": json.dumps(
                                    {
                                        "name": "computer_use.js",
                                        "arguments": {"code": "await computer.use()"},
                                    }
                                )
                            }
                        ),
                    }
                ]
            },
            source,
        )
        self.assertEqual(tool_call["name"], "js")
        self.assertEqual(tool_call["namespace"], "computer_use")
        self.assertEqual(tool_call["encrypted_function_args"], [])
        self.assertEqual(
            json.loads(tool_call["arguments"]),
            {"code": "await computer.use()"},
        )

    TOOL_SEARCH_SPEC = {
        "type": "tool_search",
        "execution": "client",
        "description": (
            "# Tool discovery\n\nSearches over deferred tool metadata.\n\n"
            "You have access to tools from the following sources:\n"
            "- github: Access repositories. " + "Say more about GitHub here. " * 20 + "\n"
            "- hotline: Look up helplines. Use it before giving one."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "number", "description": "Maximum number of tools to return."},
                "query": {"type": "string", "description": "Search query for deferred tools."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    }
    LOADED_TOOLS = [
        {
            "type": "namespace",
            "name": "mcp__fake",
            "description": "Fake tools",
            "tools": [
                {
                    "type": "function",
                    "name": "echo",
                    "description": "Echo the text back.",
                    "strict": False,
                    "parameters": {
                        "type": "object",
                        "properties": {"text": {"type": "string", "description": "What to echo."}},
                        "required": ["text"],
                        "additionalProperties": False,
                    },
                }
            ],
        }
    ]

    @staticmethod
    def _transport(call_id: str, inner: dict) -> dict:
        return {
            "type": "function_call",
            "id": f"fc_{call_id}",
            "call_id": call_id,
            "name": "run_officejs",
            "arguments": json.dumps({"code": json.dumps(inner)}),
        }

    def test_tool_search_is_in_the_catalog_with_its_sources_shortened(self):
        source = {"input": "Hi", "tools": [self.TOOL_SEARCH_SPEC]}
        self.assertEqual(excel_upstream.client_tool_types(source), {"tool_search": "tool_search"})
        catalog = excel_upstream._client_tool_protocol_instructions(source)
        self.assertIn('"type":"function","name":"tool_search"', catalog)
        self.assertIn("- github: Access repositories. Say more", catalog)
        self.assertLess(catalog.count("Say more about GitHub here."), 20)
        self.assertIn("- hotline: Look up helplines. Use it before giving one.", catalog)
        self.assertNotIn("additionalProperties", catalog)
        self.assertIn("tool_search loads more tools", catalog)

    def test_tool_search_call_reaches_codex_and_replays_as_the_native_call(self):
        source = {"input": "Hi", "tools": [self.TOOL_SEARCH_SPEC]}
        native = self._transport("call_search", {"name": "tool_search", "arguments": {"query": "github", "limit": 3}})
        call = excel_upstream.extract_native_client_tool_call({"output": [native]}, source)
        self.assertEqual(
            call,
            {
                "type": "tool_search_call",
                "id": "tsc_call_search",
                "call_id": "call_search",
                "execution": "client",
                "arguments": {"query": "github", "limit": 3},
            },
        )
        payload = excel_upstream.response_payload_with_tool_calls({"output": [native]}, [call])
        self.assertEqual(payload["output"], [{**call, "status": "completed"}])
        # Codex's own shape for a fractional limit or a missing query is not a call.
        for arguments in ({"query": "github", "limit": 2.5}, {"limit": 3}):
            half = self._transport("call_bad", {"name": "tool_search", "arguments": arguments})
            found = excel_upstream.extract_native_client_tool_call({"output": [half]}, source)
            self.assertEqual(found and found.get("arguments"), {"query": "github"} if "query" in arguments else None)

        replay = excel_upstream.translate_input_items(
            [
                {**call, "status": "completed"},
                {"type": "tool_search_output", "call_id": "call_search", "status": "completed",
                 "execution": "client", "tools": self.LOADED_TOOLS},
            ],
            excel_upstream.client_tool_types(source),
        )
        self.assertEqual(replay[0], native)
        self.assertEqual(replay[1]["type"], "function_call_output")
        self.assertEqual(replay[1]["call_id"], "call_search")
        self.assertIn('"name":"mcp__fake.echo"', replay[1]["output"])
        self.assertIn('"description":"Echo the text back."', replay[1]["output"])
        self.assertIn('"required":["text"]', replay[1]["output"])

    def test_tool_search_call_without_its_native_call_is_rebuilt(self):
        replay = excel_upstream.translate_input_items(
            [
                {"type": "tool_search_call", "call_id": "call_lost", "execution": "client",
                 "arguments": {"query": "mail"}},
                {"type": "tool_search_output", "call_id": "call_lost", "execution": "client", "tools": []},
            ]
        )
        self.assertEqual(replay[0]["name"], "run_officejs")
        self.assertEqual(replay[0]["call_id"], "call_lost")
        code = json.loads(json.loads(replay[0]["arguments"])["code"])
        self.assertEqual(code, {"name": "tool_search", "arguments": {"query": "mail"}})
        self.assertEqual(replay[1]["output"], "No matching tools found.")
        self.assertEqual(replay[1]["id"], excel_upstream.responses_replay_ids.function_item_id("call_lost"))

    def test_tools_a_search_loaded_stay_callable_but_out_of_the_catalog(self):
        history = [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Echo hi"}]},
            {"type": "tool_search_call", "call_id": "call_s", "execution": "client", "arguments": {"query": "echo"}},
            {"type": "tool_search_output", "call_id": "call_s", "execution": "client", "tools": self.LOADED_TOOLS},
        ]
        source = {"model": "gpt-6-sol-excel", "input": history, "tools": [self.TOOL_SEARCH_SPEC]}
        self.assertEqual(
            excel_upstream.client_tool_types(source),
            {"tool_search": "tool_search", "mcp__fake.echo": "function"},
        )
        body = excel_upstream.prepare_responses_body(source)
        self.assertNotIn("mcp__fake", body["input"][0]["content"][0]["text"])
        self.assertNotIn("mcp__fake", body["input"][1]["content"][0]["text"])
        # A search's result is a new round of the same user turn.
        self.assertEqual(body["metadata"]["agent_iteration"], "2")

        native = self._transport("call_echo", {"name": "mcp__fake.echo", "arguments": {"text": "hi"}})
        call = excel_upstream.extract_native_client_tool_call({"output": [native]}, source)
        self.assertEqual(call["type"], "function_call")
        self.assertEqual((call["namespace"], call["name"]), ("mcp__fake", "echo"))
        self.assertEqual(json.loads(call["arguments"]), {"text": "hi"})

    def test_other_namespaces_are_summarized_and_a_miss_returns_the_whole_tool(self):
        tool = {
            "type": "function",
            "name": "open",
            "description": "Open a panel. The calling window gets it by default. " + "More words" * 30 + ".",
            "parameters": {
                "type": "object",
                "title": "Open",
                "properties": {
                    "placement": {"type": "string", "enum": ["right", "bottom"]},
                    "target": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}, "line": {"type": "integer"}},
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Labels. Shown in the tab."},
                },
                "required": ["target"],
                "additionalProperties": False,
            },
        }
        source = {
            "input": "Hi",
            "tools": [
                {"type": "namespace", "name": "codex_app", "tools": [tool]},
                {"type": "namespace", "name": "collaboration", "tools": [{**tool, "name": "spawn_agent"}]},
            ],
        }
        catalog = excel_upstream._client_tool_protocol_instructions(source)
        summary = catalog.split("Available client tools:\n", 1)[1].split("\nRemember:", 1)[0]
        entries = json.loads(summary)
        self.assertEqual(
            entries[0],
            {
                "type": "function",
                "name": "codex_app.open",
                "summary": "Open a panel. The calling window gets it by default.",
                "parameters": {
                    "placement": '"right"|"bottom"',
                    "target": "{path: string; line?: integer}, required",
                    "tags": "string[]: Labels. Shown in the tab.",
                },
            },
        )
        # collaboration stays in full, without what only a validator reads.
        self.assertEqual(entries[1]["description"], tool["description"])
        self.assertEqual(entries[1]["parameters"]["properties"]["placement"]["enum"], ["right", "bottom"])
        self.assertNotIn("title", entries[1]["parameters"])
        self.assertNotIn("additionalProperties", json.dumps(entries[1]))
        self.assertIn("An entry with summary instead of description", catalog)

        miss = self._transport("call_miss", {"name": "codex_app.open", "arguments": {"target": {"line": 3}}})
        self.assertIsNone(excel_upstream.extract_native_client_tool_call({"output": [miss]}, source))
        replay = excel_upstream.translate_input_items(
            [miss, {"type": "function_call_output", "call_id": "call_miss", "output": "unsupported call: run_officejs"}],
            excel_upstream.client_tool_types(source),
            excel_upstream._client_tool_specs(source),
        )
        guidance = replay[1]["output"]
        self.assertIn("did not match the parameters of codex_app.open, which is defined as", guidance)
        self.assertIn('"required":["path"]', guidance)
        self.assertIn("More wordsMore words", guidance)

    def test_compact_schema_keeps_parameters_named_like_keywords(self):
        schema = {
            "type": "object",
            "title": "CreateIssue",
            "properties": {
                "title": {"type": "string", "title": "Title"},
                "additionalProperties": {"type": "boolean"},
                "labels": {"type": "array", "items": {"type": "string", "title": "Label"}},
                "state": {"type": "string", "enum": ["open", "closed"], "default": "open"},
            },
            "required": ["title"],
            "additionalProperties": False,
        }
        self.assertEqual(
            excel_upstream._compact_schema(schema),
            {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "additionalProperties": {"type": "boolean"},
                    "labels": {"type": "array", "items": {"type": "string"}},
                    "state": {"type": "string", "enum": ["open", "closed"], "default": "open"},
                },
                "required": ["title"],
            },
        )

    def test_leading_sentences_keep_whole_sentences_that_fit(self):
        cut = excel_upstream._leading_sentences
        self.assertEqual(cut("Use e.g. this one. Then more.", 20), "Use e.g. this one.")
        self.assertEqual(cut("Short.", 5 + 1), "Short.")
        self.assertEqual(cut("A" * 30, 10), "AAAAAAA...")
        self.assertEqual(cut("打开面板。然后关闭。", 6), "打开面板。")

    def test_compaction_trigger_stays_final_after_tool_reminder(self):
        body = excel_upstream.prepare_responses_body(
            {
                "model": "gpt-5.6-sol-excel",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Hello"}],
                    },
                    {"type": "compaction_trigger"},
                ],
                "tools": [{"type": "function", "name": "demo"}],
            }
        )

        self.assertEqual(body["input"][-1], {"type": "compaction_trigger"})
        self.assertEqual(body["input"][1]["role"], "developer")
        self.assertIn(
            "run_officejs",
            body["input"][1]["content"][0]["text"],
        )

    def test_catalog_is_the_only_message_without_tools(self):
        without_tools = excel_upstream.prepare_responses_body(
            {"model": "gpt-5.6-sol-excel", "input": "Hello"}
        )
        self.assertEqual(
            without_tools["input"][0]["content"][0]["text"],
            excel_upstream.EXTERNAL_CLIENT_INSTRUCTIONS,
        )
        self.assertEqual(without_tools["input"][-1]["role"], "user")

    def test_growing_conversation_keeps_a_stable_cache_prefix(self):
        source = {
            "model": "gpt-5.6-sol-excel",
            "instructions": "Be terse.",
            "prompt_cache_key": "conversation-1",
            "tools": [{"type": "function", "name": "shell_command"}],
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "list files"}],
                }
            ],
        }
        first = excel_upstream.prepare_responses_body(source)
        second = excel_upstream.prepare_responses_body(
            {
                **source,
                "input": source["input"]
                + [
                    {
                        "type": "function_call",
                        "call_id": "call_1",
                        "name": "shell_command",
                        "arguments": '{"command":"ls"}',
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_1",
                        "output": "file.txt",
                    },
                ],
            }
        )

        # The second turn must be a strict extension of the first turn. That
        # is the prefix shape the upstream prompt cache can reuse.
        self.assertEqual(second["input"][: len(first["input"])], first["input"])

    def test_client_turn_ids_do_not_break_shared_prompt_prefix(self):
        def source(turn_id, task, cache_key):
            metadata = {
                "internal_chat_message_metadata_passthrough": {
                    "turn_id": turn_id,
                }
            }
            return {
                "model": "gpt-5.6-sol-excel",
                "instructions": "You are Codex.",
                "prompt_cache_key": cache_key,
                "tools": [{"type": "function", "name": "shell_command"}],
                "input": [
                    {
                        "type": "message",
                        "role": "developer",
                        "content": [{"type": "input_text", "text": "Stable policy"}],
                        **metadata,
                    },
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Stable environment"}],
                        **metadata,
                    },
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": task}],
                        **metadata,
                    },
                ],
            }

        first = excel_upstream.prepare_responses_body(
            source("turn-one", "First task", "conversation-one")
        )
        second = excel_upstream.prepare_responses_body(
            source("turn-two", "Second task", "conversation-two")
        )

        # Instructions, the tool catalog, and the two shared source messages
        # remain a byte-identical prefix across conversations. Only the real
        # user task should be the first prompt divergence.
        self.assertEqual(first["input"][:4], second["input"][:4])
        for item in first["input"] + second["input"]:
            self.assertNotIn("internal_chat_message_metadata_passthrough", item)

    def test_client_turn_ids_stay_out_of_prompt_and_task_identity(self):
        def request(turn_id):
            return {
                "model": "gpt-5.6-sol-excel",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hello"}],
                        "internal_chat_message_metadata_passthrough": {
                            "turn_id": turn_id,
                        },
                    }
                ],
            }

        first = excel_upstream.prepare_responses_body(request("turn-one"))
        second = excel_upstream.prepare_responses_body(request("turn-two"))
        self.assertEqual(first["input"], second["input"])
        self.assertEqual(first["metadata"]["task_id"], second["metadata"]["task_id"])
        self.assertNotEqual(first["metadata"]["turn_id"], second["metadata"]["turn_id"])

    def test_client_metadata_session_is_excel_cache_key(self):
        def request(turn_id):
            return {
                "model": "gpt-5.6-sol-excel",
                "client_metadata": {"session_id": "root-session"},
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hello"}],
                        "internal_chat_message_metadata_passthrough": {
                            "turn_id": turn_id,
                        },
                    }
                ],
            }

        first = excel_upstream.prepare_responses_body(request("turn-one"))
        second = excel_upstream.prepare_responses_body(request("turn-two"))
        self.assertEqual(first["prompt_cache_key"], "root-session")
        self.assertEqual(second["prompt_cache_key"], "root-session")
        self.assertEqual(first["metadata"]["task_id"], second["metadata"]["task_id"])

    def test_catalog_position_escape_hatch_restores_suffix_layout(self):
        source = {
            "model": "gpt-5.6-sol-excel",
            "input": "Hello",
            "tools": [{"type": "function", "name": "demo"}],
        }
        original = excel_upstream.CATALOG_AT_PROMPT_END
        excel_upstream.CATALOG_AT_PROMPT_END = True
        try:
            body = excel_upstream.prepare_responses_body(source)
        finally:
            excel_upstream.CATALOG_AT_PROMPT_END = original
        self.assertEqual(len(body["input"]), 2)
        self.assertEqual(body["input"][0]["role"], "user")
        self.assertIn(
            '"name":"demo"',
            body["input"][-1]["content"][0]["text"],
        )

    def test_suffix_catalog_precedes_terminal_compaction_trigger(self):
        source = {
            "model": "gpt-5.6-sol-excel",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Hello"}],
                },
                {"type": "compaction_trigger"},
            ],
            "tools": [{"type": "function", "name": "demo"}],
        }
        original = excel_upstream.CATALOG_AT_PROMPT_END
        excel_upstream.CATALOG_AT_PROMPT_END = True
        try:
            body = excel_upstream.prepare_responses_body(source)
        finally:
            excel_upstream.CATALOG_AT_PROMPT_END = original

        self.assertEqual(body["input"][-1], {"type": "compaction_trigger"})
        self.assertIn(
            '"name":"demo"',
            body["input"][-2]["content"][0]["text"],
        )

    def test_empty_tool_output_is_rendered_as_explicit_success(self):
        items = excel_upstream.translate_input_items(
            [
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "shell_command",
                    "arguments": "{}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "",
                },
            ],
            {"shell_command": "function"},
        )
        self.assertEqual(
            items[1]["output"], "(tool call succeeded with no output)"
        )

    def test_native_plan_status_aliases_are_normalized(self):
        source = {
            "tools": [
                {
                    "type": "function",
                    "name": "update_plan",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "plan": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "step": {"type": "string"},
                                        "status": {
                                            "type": "string",
                                            "enum": [
                                                "pending",
                                                "in_progress",
                                                "completed",
                                            ],
                                        },
                                    },
                                    "required": ["step", "status"],
                                    "additionalProperties": False,
                                },
                            }
                        },
                        "required": ["plan"],
                        "additionalProperties": False,
                    },
                }
            ]
        }
        response = {
            "output": [
                {
                    "type": "function_call",
                    "name": "update_plan",
                    "arguments": json.dumps(
                        {
                            "plan": [
                                {"step": "a", "status": "done"},
                                {"step": "b", "status": "Not Started"},
                            ]
                        }
                    ),
                }
            ]
        }
        tool_call = excel_upstream.extract_native_client_tool_call(response, source)
        self.assertEqual(
            json.loads(tool_call["arguments"]),
            {
                "plan": [
                    {"step": "a", "status": "completed"},
                    {"step": "b", "status": "pending"},
                ]
            },
        )

    def test_unsupported_reasoning_effort_falls_back_to_medium(self):
        body = excel_upstream.prepare_responses_body(
            {
                "model": "gpt-5.6-sol-excel",
                "input": "Hello",
                "reasoning": {"effort": "persistent"},
            }
        )
        self.assertEqual(body["reasoning_effort"], "medium")

    def test_ultra_and_max_ask_the_backend_for_xhigh(self):
        for effort in ("ultra", "max"):
            with self.subTest(effort=effort):
                body = excel_upstream.prepare_responses_body(
                    {"model": "gpt-5.6-sol-excel", "input": "Hello", "reasoning": {"effort": effort}}
                )
                self.assertEqual(body["reasoning_effort"], "xhigh")

    def test_function_tool_marker_is_converted_only_for_allowed_tool(self):
        marker = (
            '<codex_tool_call>{"name":"codex_client__shell_command","arguments":'
            '{"command":"rg -n gpt-5.6-sol-excel excel_upstream.py"}}</codex_tool_call>'
        )
        tool_call = excel_upstream.extract_client_tool_call(
            marker,
            {"shell_command": "function"},
        )
        self.assertEqual(tool_call["type"], "function_call")
        self.assertEqual(tool_call["name"], "shell_command")
        self.assertTrue(
            tool_call["call_id"].startswith(
                excel_upstream.CLIENT_MARKER_CALL_ID_PREFIX
            )
        )
        self.assertEqual(
            json.loads(tool_call["arguments"]),
            {"command": "rg -n gpt-5.6-sol-excel excel_upstream.py"},
        )
        self.assertIsNone(
            excel_upstream.extract_client_tool_call(
                marker,
                {"update_plan": "function"},
            )
        )
        # Preserve an in-flight response generated from the pre-namespace
        # catalog while the proxy restarts.
        legacy = (
            '<codex_tool_call>{"name":"shell_command","arguments":'
            '{"command":"git status"}}</codex_tool_call>'
        )
        self.assertEqual(
            excel_upstream.extract_client_tool_call(
                legacy,
                {"shell_command": "function"},
            )["name"],
            "shell_command",
        )

    def test_custom_tool_marker_is_converted_to_custom_call(self):
        marker = (
            '<codex_tool_call>{"name":"codex_client__apply_patch",'
            '"input":"*** Begin Patch\\n*** End Patch\\n"}</codex_tool_call>'
        )
        tool_call = excel_upstream.extract_client_tool_call(
            marker,
            {"apply_patch": "custom"},
        )
        self.assertEqual(tool_call["type"], "custom_tool_call")
        self.assertEqual(tool_call["name"], "apply_patch")
        self.assertEqual(
            tool_call["input"],
            "*** Begin Patch\n*** End Patch\n",
        )

    def test_response_payload_replaces_marker_text_with_tool_call(self):
        tool_call = excel_upstream.extract_client_tool_call(
            '<codex_tool_call>{"name":"demo","arguments":{"value":1}}</codex_tool_call>',
            {"demo": "function"},
        )
        payload = excel_upstream.response_payload_with_tool_call(
            {
                "id": "resp_test",
                "object": "response",
                "status": "completed",
                "output": [{"type": "message"}],
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
            tool_call,
        )
        self.assertEqual(payload["model"], "gpt-5.6-sol-excel")
        self.assertEqual(payload["output"][0]["type"], "function_call")
        self.assertEqual(payload["usage"]["output_tokens"], 2)

    def test_native_excel_update_plan_is_normalized_to_client_schema(self):
        source = {
            "tools": [
                {
                    "type": "function",
                    "name": "update_plan",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "explanation": {"type": "string"},
                            "plan": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "step": {"type": "string"},
                                        "status": {
                                            "type": "string",
                                            "enum": [
                                                "pending",
                                                "in_progress",
                                                "completed",
                                            ],
                                        },
                                    },
                                    "required": ["step", "status"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["plan"],
                        "additionalProperties": False,
                    },
                }
            ]
        }
        response = {
            "output": [
                {"type": "reasoning"},
                {
                    "type": "function_call",
                    "id": "fc_native_plan",
                    "call_id": "call_native_plan",
                    "name": "update_plan",
                    "status": "completed",
                    "arguments": json.dumps(
                        {
                            "summary": "Inspect repository",
                            "plan": [
                                {
                                    "id": "step1",
                                    "description": "Search the code",
                                    "status": "in_progress",
                                    "result": "",
                                }
                            ],
                        }
                    ),
                    "internal_chat_message_metadata_passthrough": {
                        "turn_id": "native-plan-turn"
                    },
                },
            ]
        }
        tool_call = excel_upstream.extract_native_client_tool_call(
            response,
            source,
        )
        self.assertEqual(tool_call["name"], "update_plan")
        self.assertEqual(tool_call["id"], "fc_native_plan")
        self.assertEqual(tool_call["call_id"], "call_native_plan")
        self.assertEqual(
            json.loads(tool_call["arguments"]),
            {
                "plan": [
                    {
                        "step": "Search the code",
                        "status": "in_progress",
                    }
                ],
                "explanation": "Inspect repository",
            },
        )
        replay = excel_upstream.translate_input_items(
            [
                tool_call,
                {
                    "type": "function_call_output",
                    "call_id": "call_native_plan",
                    "output": "Plan updated",
                },
            ],
            {"update_plan": "function"},
        )
        self.assertEqual(replay[0], response["output"][1])
        self.assertEqual(replay[1]["output"], '{"status":"ok"}')
        response["output"][1]["name"] = "codex_client__update_plan"
        relayed_call = excel_upstream.extract_native_client_tool_call(
            response,
            source,
        )
        self.assertEqual(relayed_call["name"], "update_plan")
        self.assertEqual(relayed_call["arguments"], tool_call["arguments"])

    def test_unknown_native_excel_tool_is_not_forwarded(self):
        self.assertIsNone(
            excel_upstream.extract_native_client_tool_call(
                {
                    "output": [
                        {
                            "type": "function_call",
                            "name": "list_skills",
                            "arguments": "{}",
                        }
                    ]
                },
                {
                    "tools": [
                        {
                            "type": "function",
                            "name": "shell_command",
                            "parameters": {"type": "object"},
                        }
                    ]
                },
            )
        )

    def test_local_model_is_merged_once(self):
        payload = excel_upstream.merge_local_models_payload(
            {"object": "list", "data": [{"id": "gpt-5.5", "object": "model"}]}
        )
        payload = excel_upstream.merge_local_models_payload(payload)
        self.assertEqual(
            [item["id"] for item in payload["data"]],
            [
                "gpt-5.5",
                "gpt-5.6-luna-excel",
                "gpt-5.6-terra-excel",
                "gpt-5.6-sol-excel",
                "gpt-6-sol-excel",
                "gpt-6-luna-excel",
                "gpt-6-astra-excel",
                "gpt-5.6-luna-1m-excel",
                "gpt-5.6-terra-1m-excel",
                "gpt-5.6-sol-1m-excel",
                "gpt-6-sol-1m-excel",
                "gpt-6-luna-1m-excel",
                "gpt-6-astra-1m-excel",
            ],
        )

class ExcelStreamTransformTests(unittest.TestCase):
    SOURCE_BODY = {
        "tools": [
            {
                "type": "function",
                "name": "shell_command",
                "parameters": {"type": "object"},
            }
        ]
    }

    @staticmethod
    def _sse(event: str, payload: dict) -> bytes:
        return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode()

    def _collect(
        self,
        chunks: list[bytes],
        source_body: dict | None = None,
    ) -> list[tuple[str, dict]]:
        from excel_codex_bridge import server as proxy_module

        transform = proxy_module._excel_tool_stream_transform(
            source_body if source_body is not None else self.SOURCE_BODY
        )

        async def source():
            for chunk in chunks:
                yield chunk

        async def run():
            return [chunk async for chunk in transform(source())]

        raw = b"".join(asyncio.run(run())).decode()
        events = []
        for block in raw.split("\n\n"):
            if not block.strip():
                continue
            name = None
            data_lines = []
            for line in block.split("\n"):
                if line.startswith("event:"):
                    name = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())
            data = "\n".join(data_lines)
            events.append((name, json.loads(data) if data != "[DONE]" else {}))
        return events

    def _delta_chunks(self, text: str, size: int = 9) -> list[bytes]:
        return [
            self._sse(
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "item_id": "msg_1",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": text[start : start + size],
                },
            )
            for start in range(0, len(text), size)
        ]

    def _stream(self, text: str) -> list[bytes]:
        return (
            [
                self._sse(
                    "response.created",
                    {"type": "response.created", "response": {"id": "resp_1"}},
                )
            ]
            + self._delta_chunks(text)
            + [
                self._sse(
                    "response.output_text.done",
                    {
                        "type": "response.output_text.done",
                        "item_id": "msg_1",
                        "text": text,
                    },
                ),
                self._sse(
                    "response.output_item.done",
                    {
                        "type": "response.output_item.done",
                        "item": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": text}],
                        },
                    },
                ),
                self._sse(
                    "response.completed",
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp_1",
                            "object": "response",
                            "status": "completed",
                            "model": "gpt-5.5",
                            "output": [
                                {
                                    "type": "message",
                                    "role": "assistant",
                                    "content": [
                                        {"type": "output_text", "text": text}
                                    ],
                                }
                            ],
                            "usage": {"input_tokens": 5, "output_tokens": 7},
                        },
                    },
                ),
            ]
        )

    def test_marker_stream_is_converted_to_tool_call_events(self):
        marker = (
            '<codex_tool_call>{"name":"shell_command","arguments":'
            '{"command":"ls"}}</codex_tool_call>'
        )
        events = self._collect(self._stream(marker))
        names = [name for name, _ in events]

        self.assertNotIn("response.output_text.delta", names)
        self.assertNotIn("response.output_text.done", names)
        self.assertIn("response.function_call_arguments.done", names)
        completed = dict(events)[
            "response.completed"
        ]["response"]
        self.assertEqual(completed["model"], "gpt-5.6-sol-excel")
        self.assertEqual(completed["output"][0]["type"], "function_call")
        self.assertEqual(completed["output"][0]["name"], "shell_command")
        self.assertEqual(completed["usage"]["output_tokens"], 7)

    def test_run_officejs_stream_is_converted_to_client_tool_events(self):
        native_item = {
            "type": "function_call",
            "id": "fc_transport_stream",
            "call_id": "call_transport_stream",
            "name": "run_officejs",
            "status": "completed",
            "arguments": json.dumps(
                {
                    "summary": "List files",
                    "extended_summary": "Inspect the repository",
                    "code": json.dumps(
                        {
                            "name": "shell_command",
                            "arguments": {"command": "Get-ChildItem"},
                        },
                        separators=(",", ":"),
                    ),
                    "destructive": False,
                    "references": [],
                },
                separators=(",", ":"),
            ),
        }
        chunks = [
            self._sse(
                "response.created",
                {"type": "response.created", "response": {"id": "resp_transport"}},
            ),
            self._sse(
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "output_index": 1,
                    "item": native_item,
                },
            ),
            self._sse(
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_transport",
                        "status": "completed",
                        "model": "gpt-5.5",
                        "output": [
                            {
                                "type": "message",
                                "role": "assistant",
                                "phase": "commentary",
                                "content": [
                                    {
                                        "type": "output_text",
                                        "text": "I am inspecting now.",
                                    }
                                ],
                            },
                            native_item,
                        ],
                        "usage": {"input_tokens": 5, "output_tokens": 7},
                    },
                },
            ),
        ]
        events = self._collect(chunks)
        completed = dict(events)["response.completed"]["response"]
        self.assertEqual(completed["output"][0]["type"], "message")
        self.assertEqual(completed["output"][1]["type"], "function_call")
        self.assertEqual(completed["output"][1]["name"], "shell_command")
        self.assertEqual(
            json.loads(completed["output"][1]["arguments"]),
            {"command": "Get-ChildItem"},
        )
        self.assertNotIn("run_officejs", json.dumps(events))

    def test_tool_search_streams_as_a_finished_item(self):
        source = {"tools": [ExcelUpstreamTests.TOOL_SEARCH_SPEC]}
        native_item = ExcelUpstreamTests._transport(
            "call_stream_search", {"name": "tool_search", "arguments": {"query": "gmail"}}
        )
        chunks = [
            self._sse("response.created", {"type": "response.created", "response": {"id": "resp_s"}}),
            self._sse(
                "response.output_item.done",
                {"type": "response.output_item.done", "output_index": 0, "item": native_item},
            ),
            self._sse(
                "response.completed",
                {"type": "response.completed",
                 "response": {"id": "resp_s", "status": "completed", "output": [native_item]}},
            ),
        ]
        events = self._collect(chunks, source)
        names = [name for name, _ in events]
        self.assertEqual(names, ["response.created", "response.output_item.added",
                                 "response.output_item.done", "response.completed"])
        done = dict(events)["response.output_item.done"]["item"]
        self.assertEqual(done["type"], "tool_search_call")
        self.assertEqual(done["arguments"], {"query": "gmail"})
        self.assertEqual(done["status"], "completed")
        self.assertNotIn("run_officejs", json.dumps(events))

    def test_native_client_tool_keeps_upstream_output_index_after_reasoning(self):
        native_item = {
            "type": "function_call",
            "id": "fc_transport_question",
            "call_id": "call_transport_question",
            "name": "run_officejs",
            "status": "completed",
            "arguments": json.dumps(
                {
                    "summary": "Ask setup questions",
                    "extended_summary": "Collect the required workbook setup choices",
                    "code": json.dumps(
                        {
                            "name": "request_user_input",
                            "arguments": {
                                "questions": [
                                    {
                                        "id": "mode",
                                        "header": "Mode",
                                        "question": "Which mode should I use?",
                                        "options": [
                                            {
                                                "label": "Default",
                                                "description": "Use the standard mode.",
                                            },
                                            {
                                                "label": "Advanced",
                                                "description": "Use advanced controls.",
                                            },
                                        ],
                                    }
                                ]
                            },
                        },
                        separators=(",", ":"),
                    ),
                    "destructive": False,
                    "references": [],
                },
                separators=(",", ":"),
            ),
        }
        reasoning_item = {
            "type": "reasoning",
            "id": "rs_before_question",
            "summary": [],
        }
        chunks = [
            self._sse(
                "response.created",
                {"type": "response.created", "response": {"id": "resp_question"}},
            ),
            self._sse(
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": reasoning_item,
                },
            ),
            self._sse(
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": reasoning_item,
                },
            ),
            self._sse(
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "output_index": 1,
                    "item": native_item,
                },
            ),
            self._sse(
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_question",
                        "status": "completed",
                        "model": "gpt-5.5",
                        "output": [reasoning_item, native_item],
                    },
                },
            ),
        ]

        events = self._collect(
            chunks,
            {
                "tools": [
                    {
                        "type": "function",
                        "name": "request_user_input",
                        "parameters": {"type": "object"},
                    }
                ]
            },
        )
        tool_events = [
            payload
            for name, payload in events
            if name
            in {
                "response.output_item.added",
                "response.function_call_arguments.delta",
                "response.function_call_arguments.done",
                "response.output_item.done",
            }
            and payload.get("output_index") == 1
        ]

        self.assertEqual(len(tool_events), 4)
        self.assertTrue(all(event["output_index"] == 1 for event in tool_events))
        self.assertEqual(tool_events[-1]["item"]["name"], "request_user_input")
        completed = dict(events)["response.completed"]["response"]
        self.assertEqual(completed["output"][0]["type"], "reasoning")
        self.assertEqual(completed["output"][1]["name"], "request_user_input")
        self.assertNotIn("run_officejs", json.dumps(events))

    def test_parallel_native_calls_reach_codex_as_parallel_calls(self):
        def transport(call_id, cmd):
            return {
                "type": "function_call",
                "id": f"fc_{call_id}",
                "call_id": call_id,
                "name": "run_officejs",
                "status": "completed",
                "arguments": json.dumps(
                    {"code": json.dumps({"name": "exec_command", "arguments": {"cmd": cmd}})}
                ),
            }

        first, second = transport("call_a", "pwd"), transport("call_b", "ls")
        chunks = [
            self._sse("response.created", {"type": "response.created", "response": {"id": "resp_two"}}),
        ]
        for index, item in enumerate((first, second)):
            for event in ("response.output_item.added", "response.output_item.done"):
                chunks.append(self._sse(event, {"type": event, "output_index": index, "item": item}))
        chunks.append(
            self._sse(
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {"id": "resp_two", "status": "completed", "output": [first, second]},
                },
            )
        )

        events = self._collect(
            chunks,
            {"tools": [{"type": "function", "name": "exec_command", "parameters": {"type": "object"}}]},
        )

        done_events = [payload for name, payload in events if name == "response.output_item.done"]
        self.assertEqual([event["item"]["call_id"] for event in done_events], ["call_a", "call_b"])
        self.assertEqual([event["output_index"] for event in done_events], [0, 1])
        self.assertEqual(
            [json.loads(event["item"]["arguments"]) for event in done_events],
            [{"cmd": "pwd"}, {"cmd": "ls"}],
        )
        completed = dict(events)["response.completed"]["response"]
        self.assertEqual([item["call_id"] for item in completed["output"]], ["call_a", "call_b"])
        self.assertEqual([name for name, _ in events].count("response.completed"), 1)
        self.assertNotIn("run_officejs", json.dumps(events))

    def test_plain_text_streams_through_incrementally(self):
        text = "The answer is 42, see <codex spreadsheet notes for details."
        events = self._collect(self._stream(text))
        deltas = [
            payload["delta"]
            for name, payload in events
            if name == "response.output_text.delta"
        ]
        self.assertEqual("".join(deltas), text)
        completed = dict(events)["response.completed"]["response"]
        self.assertEqual(completed["output"][0]["type"], "message")
        done_payload = dict(events)["response.output_text.done"]
        self.assertEqual(done_payload["text"], text)

    def test_repeated_plan_update_is_not_rewritten_as_assistant_text(self):
        plan_arguments = json.dumps(
            {"plan": [{"step": "Inspect code", "status": "in_progress"}]}
        )
        source_body = {
            "tools": [
                {
                    "type": "function",
                    "name": "update_plan",
                    "parameters": {"type": "object"},
                }
            ],
            "input": [
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "update_plan",
                    "arguments": plan_arguments,
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "Plan updated",
                },
            ],
        }
        marker = (
            '<codex_tool_call>{"name":"codex_client__update_plan","arguments":'
            + plan_arguments
            + "}</codex_tool_call>"
        )
        events = self._collect(self._stream(marker), source_body)
        names = [name for name, _ in events]

        self.assertIn("response.function_call_arguments.done", names)
        completed = dict(events)["response.completed"]["response"]
        self.assertEqual(completed["output"][0]["type"], "function_call")
        self.assertEqual(completed["output"][0]["name"], "update_plan")

    def test_native_tool_call_is_normalized_without_losing_identity(self):
        source_body = {
            "tools": [
                {
                    "type": "function",
                    "name": "update_plan",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "plan": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "step": {"type": "string"},
                                        "status": {
                                            "type": "string",
                                            "enum": [
                                                "pending",
                                                "in_progress",
                                                "completed",
                                            ],
                                        },
                                    },
                                    "required": ["step", "status"],
                                    "additionalProperties": False,
                                },
                            }
                        },
                        "required": ["plan"],
                        "additionalProperties": False,
                    },
                }
            ],
            "input": [],
        }
        raw_arguments = json.dumps(
            {
                "plan": [
                    {
                        "id": "step1",
                        "description": "Inspect the repository",
                        "status": "in_progress",
                        "result": "",
                    }
                ]
            }
        )
        native_item = {
            "type": "function_call",
            "id": "fc_upstream",
            "call_id": "call_upstream",
            "name": "update_plan",
            "arguments": raw_arguments,
        }
        chunks = [
            self._sse(
                "response.created",
                {"type": "response.created", "response": {"id": "resp_1"}},
            ),
            self._sse(
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {**native_item, "arguments": ""},
                },
            ),
            self._sse(
                "response.function_call_arguments.delta",
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": "fc_upstream",
                    "delta": raw_arguments,
                },
            ),
            self._sse(
                "response.function_call_arguments.done",
                {
                    "type": "response.function_call_arguments.done",
                    "item_id": "fc_upstream",
                    "arguments": raw_arguments,
                },
            ),
            self._sse(
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": native_item,
                },
            ),
            self._sse(
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_1",
                        "status": "completed",
                        "model": "gpt-5.5",
                        "output": [native_item],
                        "usage": {"input_tokens": 5, "output_tokens": 7},
                    },
                },
            ),
        ]
        events = self._collect(chunks, source_body)

        item_done_payloads = [
            payload
            for name, payload in events
            if name == "response.output_item.done"
            and payload.get("item", {}).get("type") == "function_call"
        ]
        self.assertEqual(len(item_done_payloads), 1)
        arguments = json.loads(item_done_payloads[0]["item"]["arguments"])
        self.assertEqual(
            arguments,
            {"plan": [{"step": "Inspect the repository", "status": "in_progress"}]},
        )
        completed = dict(events)["response.completed"]["response"]
        self.assertEqual(completed["model"], "gpt-5.6-sol-excel")
        self.assertEqual(completed["output"][0]["type"], "function_call")
        self.assertEqual(completed["output"][0]["id"], "fc_upstream")
        self.assertEqual(completed["output"][0]["call_id"], "call_upstream")

    def test_marker_without_valid_tool_is_released_as_text(self):
        marker = (
            '<codex_tool_call>{"name":"unknown_tool","arguments":{}}'
            "</codex_tool_call>"
        )
        events = self._collect(self._stream(marker))
        deltas = [
            payload["delta"]
            for name, payload in events
            if name == "response.output_text.delta"
        ]
        self.assertEqual("".join(deltas), marker)
        completed = dict(events)["response.completed"]["response"]
        self.assertEqual(completed["output"][0]["type"], "message")


class ExcelSessionPersistenceTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows DPAPI test")
    def test_session_is_encrypted_and_reloaded_with_dpapi(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "excel-session.dpapi")
            token = _jwt_with_exp(time.time() + 600)
            store = excel_upstream.ExcelSessionStore(path)
            status = store.configure(
                {
                    "authorization": f"Bearer {token}",
                    "chatgpt-account-id": "account-1",
                },
                tools_version_id="tools-excel-core-test",
            )

            self.assertTrue(status["persisted"])
            with open(path, "rb") as handle:
                protected = handle.read()
            self.assertNotIn(token.encode(), protected)
            self.assertNotIn(b"account-1", protected)

            restored = excel_upstream.ExcelSessionStore(path)
            restored_status = restored.load()
            self.assertTrue(restored_status["configured"])
            self.assertTrue(restored_status["persisted"])
            self.assertEqual(
                restored.request_headers(stream=False)["chatgpt-account-id"],
                "account-1",
            )
            self.assertEqual(
                restored.tools_version_id(),
                "tools-excel-core-test",
            )



EXEC_TOOL = {
    "type": "custom",
    "name": "exec",
    "description": "Run JavaScript code to orchestrate/compose tool calls\n"
    "declare const tools: { exec_command(args: { cmd: string; }): Promise<unknown>; };",
    "format": {"type": "grammar", "syntax": "lark", "definition": "start: SOURCE\nSOURCE: /[\\s\\S]+/\n"},
}
WAIT_TOOL = {"type": "function", "name": "wait", "strict": False,
             "parameters": {"type": "object", "properties": {"cell_id": {"type": "string"}},
                            "required": ["cell_id"]}}
SPAWN_TOOL = {"type": "function", "name": "spawn_agent", "strict": False,
              "parameters": {"type": "object", "properties": {"message": {"type": "string"}}}}


def lite_body(*history: dict, **extra) -> dict:
    """A request the way Codex sends it with Responses Lite (its own entries for gpt-5.6 and gpt-6)."""
    return {
        "model": "gpt-5.6-sol",
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "input": [
            {"type": "additional_tools", "id": "at_1", "role": "developer", "tools": [
                {"type": "namespace", "name": "functions", "description": "", "tools": [EXEC_TOOL, WAIT_TOOL]},
                {"type": "namespace", "name": "collaboration", "description": "Sub-agents.",
                 "tools": [SPAWN_TOOL]},
            ]},
            {"type": "message", "id": "msg_base", "role": "developer",
             "content": [{"type": "input_text", "text": "You are Codex, based on GPT-5."}]},
            {"type": "message", "role": "developer",
             "content": [{"type": "input_text", "text": "<permissions instructions>"}]},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Run the check."}]},
            *history,
        ],
        **extra,
    }


class ResponsesLiteTests(unittest.TestCase):
    def test_tools_and_instructions_come_out_of_the_input(self):
        body = excel_upstream.from_responses_lite(lite_body())

        self.assertEqual(body["instructions"], "You are Codex, based on GPT-5.")
        self.assertEqual(
            body["tools"],
            [EXEC_TOOL, WAIT_TOOL, {"type": "namespace", "name": "collaboration", "description": "Sub-agents.",
                                    "tools": [SPAWN_TOOL]}],
        )
        self.assertEqual(
            [part["text"] for item in body["input"] for part in item["content"]],
            ["<permissions instructions>", "Run the check."],
        )
        self.assertEqual(
            excel_upstream.client_tool_types(body),
            {"exec": "custom", "wait": "function", "collaboration.spawn_agent": "function"},
        )
        self.assertFalse(body["parallel_tool_calls"])

    def test_other_requests_are_left_as_they_are(self):
        body = {"model": "gpt-5.6-sol-excel", "input": "hi", "tools": [WAIT_TOOL], "instructions": "Be brief."}
        self.assertIs(excel_upstream.from_responses_lite(body), body)
        listed = {"model": "gpt-5.6-sol", "tools": [WAIT_TOOL], "input": lite_body()["input"][1:]}
        self.assertIs(excel_upstream.from_responses_lite(listed), listed)

    def test_tools_already_there_are_kept_and_not_repeated(self):
        own_wait = {**WAIT_TOOL, "description": "the request's own"}
        body = excel_upstream.from_responses_lite(
            lite_body(tools=[own_wait], instructions="Given instructions."))

        self.assertEqual([tool["name"] for tool in body["tools"]], ["wait", "exec", "collaboration"])
        self.assertEqual(body["tools"][0], own_wait)
        # Instructions already given stay; the developer message stays in the conversation too.
        self.assertEqual(body["instructions"], "Given instructions.")
        self.assertEqual(body["input"][0]["id"], "msg_base")

    def test_the_prepared_request_reads_like_one_with_tools(self):
        lite = excel_upstream.prepare_responses_body(excel_upstream.from_responses_lite(lite_body()))
        usual = excel_upstream.prepare_responses_body({
            "model": "gpt-5.6-sol",
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "instructions": "You are Codex, based on GPT-5.",
            "tools": [EXEC_TOOL, WAIT_TOOL, {"type": "namespace", "name": "collaboration",
                                             "description": "Sub-agents.", "tools": [SPAWN_TOOL]}],
            "input": lite_body()["input"][2:],
        })

        self.assertEqual(lite, usual)
        self.assertNotIn("additional_tools", json.dumps(lite))
        catalog = lite["input"][1]["content"][0]["text"]
        self.assertIn('{"type":"custom","name":"exec"', catalog)
        self.assertIn("code mode", catalog)
        self.assertIn(excel_upstream.CODE_MODE_EXAMPLE, catalog)
        reminder = lite["input"][2]["content"][0]["text"]
        self.assertIn(excel_upstream.CODE_MODE_EXAMPLE, reminder)
        self.assertIn("transport exec", reminder)

    def test_an_exec_call_through_the_transport_becomes_codex_exec(self):
        source = excel_upstream.from_responses_lite(lite_body())
        script = 'const r = await tools.exec_command({cmd: "pwd"});\ntext(r.output);'
        response = {"output": [{
            "type": "function_call", "id": "fc_exec", "call_id": "call_exec", "name": "run_officejs",
            "arguments": json.dumps({"code": json.dumps({"name": "exec", "input": script})}),
        }]}

        calls = excel_upstream.extract_native_client_tool_calls(response, source)

        self.assertEqual(len(calls), 1)
        self.assertEqual((calls[0]["type"], calls[0]["name"], calls[0]["input"]), ("custom_tool_call", "exec", script))
        self.assertNotIn("namespace", calls[0])

    def test_functions_prefix_names_the_plain_tool(self):
        source = excel_upstream.from_responses_lite(lite_body())
        response = {"output": [{
            "type": "function_call", "id": "fc_wait", "call_id": "call_wait", "name": "run_officejs",
            "arguments": json.dumps({"code": json.dumps({"name": "functions.wait",
                                                         "arguments": {"cell_id": "1"}})}),
        }]}

        calls = excel_upstream.extract_native_client_tool_calls(response, source)

        self.assertEqual([(call["type"], call["name"]) for call in calls], [("function_call", "wait")])

    def _rejected(self, code: dict, allowed_tools: dict) -> str:
        replay = excel_upstream.translate_input_items(
            [
                {"type": "function_call", "call_id": "call_no_tool", "name": "run_officejs",
                 "arguments": json.dumps({"code": json.dumps(code)})},
                {"type": "function_call_output", "call_id": "call_no_tool",
                 "output": "unsupported call: run_officejs"},
            ],
            allowed_tools,
        )
        return replay[1]["output"]

    def test_a_tool_called_directly_in_code_mode_is_pointed_at_exec(self):
        said = self._rejected({"name": "exec_command", "arguments": {"cmd": "pwd"}},
                              {"exec": "custom", "wait": "function"})
        self.assertIn("exec_command is not a tool in the catalog: Codex runs in code mode here", said)
        self.assertIn(excel_upstream.CODE_MODE_EXAMPLE, said)

    def test_exec_without_code_mode_is_pointed_at_the_catalog_tools(self):
        # A conversation begun in code mode (Codex's own entries), carried on with the bridge's.
        for shell in ("exec_command", "shell_command"):
            with self.subTest(shell=shell):
                said = self._rejected({"name": "exec", "input": "text(1)"},
                                      {shell: "function", "apply_patch": "custom"})
                self.assertIn("exec is not a tool in the catalog: it belongs to Codex's code mode", said)
                self.assertIn(f"({shell} for shell commands)", said)

    def test_catalog_text_without_code_mode_is_unchanged(self):
        source = {"model": "gpt-5.6-sol-excel", "input": "hi", "tools": [
            {"type": "function", "name": "exec_command", "parameters": {"type": "object"}},
            {"type": "custom", "name": "apply_patch"},
        ]}
        catalog = excel_upstream._client_tool_protocol_instructions(source)
        reminder = excel_upstream._client_tool_protocol_reminder(source)

        self.assertIn(
            "catalog contains a suitable tool. For repository inspection, invoke a suitable catalog shell tool "
            "(for example exec_command) through run_officejs. Transport has two layers",
            catalog,
        )
        self.assertIn(
            '{"name":"TOOL_NAME","input":"RAW_INPUT"}. Do not put JavaScript, OfficeJS, a second run_officejs '
            "envelope, or a functions.run_officejs wrapper inside code. The field is named code for "
            "compatibility; it is not JavaScript. Serialize the complete inner object",
            catalog,
        )
        self.assertNotIn("code mode", catalog + reminder)
        self.assertIn(
            "The code field is not JavaScript; serialize the inner JSON and escape backslashes and quotes in "
            'shell commands. Example inner code: {"name":"exec_command","arguments":{"cmd":"pwd"}}. Do not merely',
            reminder,
        )


if __name__ == "__main__":
    unittest.main()

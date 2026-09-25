from __future__ import annotations

import asyncio
import gzip
import json
import subprocess
import time
import zlib
from types import SimpleNamespace

import httpx
import pytest
import zstandard

from excel_codex_bridge import cli, excel_upstream, sub2api, sub2api_cli
from helpers import session_headers, write_codex_login, write_webview_session
from test_server import text_stream

API_KEY = "test-api-" + "a" * 40
ADMIN_KEY = "test-admin-" + "b" * 40


class Harness:
    def __init__(self, handler=None):
        self.requests = []

        def record(request):
            self.requests.append(request)
            if handler:
                return handler(request)
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content=text_stream("pong"))

        self.app = sub2api.create_app(
            sub2api.GatewayKeys(API_KEY, ADMIN_KEY),
            client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(record)),
        )

    def request(self, method, path, *, peer="172.22.0.2", key=API_KEY, headers=None, **kwargs):
        async def run():
            auth = {"authorization": f"Bearer {key}"} if key else {}
            transport = httpx.ASGITransport(app=self.app, client=(peer, 54321))
            async with httpx.AsyncClient(transport=transport, base_url="http://excel-sub2api:8000") as client:
                return await client.request(method, path, headers={**auth, **(headers or {})}, **kwargs)
        return asyncio.run(run())

    def import_session(self, **kwargs):
        return self.request("POST", sub2api.SESSION_PATH, peer="127.0.0.1", key=ADMIN_KEY,
                            json=kwargs or {"headers": session_headers()})


def test_health_is_liveness_only_and_models_need_key():
    harness = Harness()
    assert harness.request("GET", "/healthz", key=None).json() == {"ok": True}
    assert harness.request("GET", "/models", key=None).status_code == 401
    assert harness.request("GET", "/models", key=ADMIN_KEY).status_code == 401
    response = harness.request("GET", "/v1/models")
    assert {m["id"] for m in response.json()["data"]} == set(excel_upstream.MODEL_IDS)
    assert not harness.requests


@pytest.mark.parametrize("peer", ["172.22.0.2", "203.0.113.8", "::ffff:203.0.113.8"])
def test_admin_requires_real_loopback_not_forwarded_headers(peer):
    response = Harness().request(
        "POST", sub2api.SESSION_PATH, peer=peer, key=ADMIN_KEY,
        headers={"x-forwarded-for": "127.0.0.1", "forwarded": "for=127.0.0.1"},
        json={"headers": session_headers()},
    )
    assert response.status_code == 404


@pytest.mark.parametrize("key", [None, API_KEY, "incorrect"])
def test_admin_requires_separate_key_even_from_loopback(key):
    assert Harness().request("GET", sub2api.SESSION_PATH, peer="::1", key=key).status_code == 401


@pytest.mark.parametrize("path", ["/healthz", "/v1/models", "/admin/session"])
def test_browser_requests_rejected(path):
    assert Harness().request("GET", path, headers={"origin": "https://example.com"}).status_code == 403


@pytest.mark.parametrize("path", ["/docs", "/openapi.json", "/v1/chat/completions",
                                  "/v1/responses/compact", "/api/config/excel-session"])
def test_unimplemented_protocols_are_not_exposed(path):
    assert Harness().request("POST", path, json={}).status_code == 404


def test_session_lifecycle_is_memory_only_and_never_returns_secrets():
    harness = Harness()
    assert harness.import_session().json()["configured"] is True
    assert harness.app.app.state.bridge.reader.store.status()["storage"] == "memory-only"
    status = harness.request("GET", sub2api.SESSION_PATH, peer="::1", key=ADMIN_KEY)
    assert set(status.json()) == {"configured", "expired", "expires_at", "expires_in_seconds"}
    for secret in (API_KEY, ADMIN_KEY, "account-id", "Bearer", "headers"):
        assert secret not in status.text
    assert harness.request("DELETE", sub2api.SESSION_PATH, peer="127.0.0.1",
                           key=ADMIN_KEY).json()["configured"] is False
    result = harness.request("POST", "/responses", json={"model": excel_upstream.MODEL_ID, "input": "hi"})
    assert result.status_code == 401
    assert not harness.requests


@pytest.mark.parametrize("headers", [
    {}, {"authorization": "Bearer secret"}, session_headers(time.time() - 3600),
    {**session_headers(), "x-openai-account-id": "wrong-account"},
    {**session_headers(), "x-test": "injected\r\nheader"},
])
def test_bad_imports_do_not_replace_valid_session(headers):
    harness = Harness()
    assert harness.import_session().status_code == 200
    before = harness.app.app.state.bridge.reader.store.request_headers(stream=False)
    response = harness.import_session(headers=headers)
    assert response.status_code == 400
    assert harness.app.app.state.bridge.reader.store.request_headers(stream=False) == before
    assert "secret" not in response.text


@pytest.mark.parametrize("stream", [True, False])
def test_responses_use_excel_session_not_gateway_key(stream):
    harness = Harness()
    assert harness.import_session().status_code == 200
    response = harness.request("POST", "/v1/responses", json={
        "model": excel_upstream.MODEL_ID, "input": "hello", "stream": stream,
    })
    assert response.status_code == 200
    assert "pong" in response.text
    if stream:
        assert "response.completed" in response.text
        assert response.headers["x-accel-buffering"] == "no"
    else:
        assert response.json()["model"] == excel_upstream.MODEL_ID
    assert len(harness.requests) == 1
    request = harness.requests[0]
    assert str(request.url) == excel_upstream.RESPONSES_URL
    assert API_KEY not in str(request.headers)
    assert ADMIN_KEY not in str(request.headers)
    assert request.headers["chatgpt-account-id"] == "account-id"


def test_upstream_status_and_retry_after_are_preserved():
    harness = Harness(lambda _: httpx.Response(429, json={"error": {"message": "quota"}},
                                                headers={"retry-after": "60"}))
    harness.import_session()
    response = harness.request("POST", "/responses", json={"model": excel_upstream.MODEL_ID, "input": "hi"})
    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"


def test_unknown_model_and_non_boolean_stream_never_reach_upstream():
    harness = Harness()
    harness.import_session()
    for body in ({"model": "not-an-excel-model"}, {"model": excel_upstream.MODEL_ID, "stream": "false"}):
        assert harness.request("POST", "/responses", json=body).status_code == 400
    assert not harness.requests


@pytest.mark.parametrize("encoding,compress", [
    ("gzip", gzip.compress), ("deflate", zlib.compress),
    ("zstd", zstandard.ZstdCompressor().compress),
])
def test_compressed_bodies_are_bounded_before_json_parse(encoding, compress):
    payload = {"input": "x" * 4096}
    raw = compress(json.dumps(payload).encode())
    assert sub2api.decode_json(raw, encoding, 8192) == payload
    with pytest.raises(sub2api.BodyTooLarge):
        sub2api.decode_json(raw, encoding, 128)


def test_body_errors_are_sanitized_and_limits_apply(monkeypatch):
    monkeypatch.setattr(sub2api, "MAX_BODY_BYTES", 128)
    harness = Harness()
    assert harness.request("POST", "/responses", content=b"x" * 129).status_code == 413
    assert harness.request("POST", "/responses", content=gzip.compress(b"x" * 4096),
                           headers={"content-encoding": "gzip"}).status_code == 413
    response = harness.request("POST", "/responses", content=b"private-sentinel")
    assert response.status_code == 400
    assert "private-sentinel" not in response.text


def test_secret_configuration_fails_closed_and_redacts(monkeypatch, tmp_path):
    name = "EXCEL_SUB2API_API_KEY"
    monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(name + "_FILE", raising=False)
    with pytest.raises(ValueError):
        sub2api.read_secret(name)
    secret = tmp_path / "key"
    secret.write_text(API_KEY + "\n")
    monkeypatch.setenv(name + "_FILE", str(secret))
    assert sub2api.read_secret(name) == API_KEY
    monkeypatch.setenv(name, ADMIN_KEY)
    with pytest.raises(ValueError):
        sub2api.read_secret(name)
    with pytest.raises(ValueError):
        sub2api.GatewayKeys(API_KEY, API_KEY)
    with pytest.raises(ValueError):
        sub2api.GatewayKeys("short", ADMIN_KEY)
    assert API_KEY not in repr(sub2api.GatewayKeys(API_KEY, ADMIN_KEY))


def test_secret_initialization_is_random_and_never_overwrites(tmp_path):
    target = tmp_path / "secrets"
    sub2api_cli.init_secrets(target)
    api, admin = (target / "api-key").read_text(), (target / "admin-key").read_text()
    assert api != admin
    assert len(api.strip()) >= 32
    with pytest.raises(ValueError):
        sub2api_cli.init_secrets(target)
    assert (target / "api-key").read_text() == api


@pytest.mark.parametrize("target", ["-oProxyCommand=bad", "host;id", "host $(id)", "user@host\nwhoami"])
def test_ssh_destination_rejects_command_injection(target):
    with pytest.raises(SystemExit):
        sub2api_cli._parser().parse_args(["push-session", "--ssh=" + target])


def test_ssh_sync_uses_stdin_not_arguments_or_logs(monkeypatch, tmp_path):
    from excel_codex_bridge.session import SessionReader
    root = write_webview_session(tmp_path / "Office", time.time() + 3600)
    reader = SessionReader(webview_root=root)
    args = sub2api_cli._parser().parse_args(["push-session", "--ssh", "operator@my-vps", "--sudo"])
    captured = {}

    def run(command, **kwargs):
        captured.update(command=command, **kwargs)
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "configured": True, "expired": False, "headers": "should-not-be-returned"
        }).encode())

    monkeypatch.setattr(subprocess, "run", run)
    assert sub2api_cli.push_session(args, reader)["configured"]
    payload = json.loads(captured["input"])
    assert payload["headers"]["authorization"].startswith("Bearer ")
    assert payload["headers"]["authorization"] not in repr(captured["command"])
    assert captured["command"][-1].startswith("sudo -n docker exec -i excel-sub2api")
    assert "StrictHostKeyChecking=no" not in repr(captured["command"])
    assert "shell" not in captured


def test_push_session_builds_reader_with_login_choice(monkeypatch):
    seen = {}

    class FakeReader:
        def __init__(self, *args, **kwargs):
            seen["login"] = kwargs.get("login")

    monkeypatch.setattr(sub2api_cli, "SessionReader", FakeReader)
    monkeypatch.setattr(sub2api_cli, "push_session",
                        lambda args, reader: {"configured": True, "expired": False})
    assert sub2api_cli.main(["push-session", "--ssh", "operator@my-vps", "--login", "codex"]) == 0
    assert seen["login"] == "codex"


def test_push_session_sends_codex_sign_in_without_excel(monkeypatch, tmp_path):
    from excel_codex_bridge.session import SessionReader
    # A machine with no Excel session anywhere; only Codex's own login exists.
    auth = write_codex_login(tmp_path / "auth.json", time.time() + 3600, account="codex-acct")
    reader = SessionReader(login="codex", codex_auth=auth)
    args = sub2api_cli._parser().parse_args(
        ["push-session", "--ssh", "operator@my-vps", "--login", "codex"]
    )
    assert args.login == "codex"
    captured = {}

    def run(command, **kwargs):
        captured.update(command=command, **kwargs)
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "configured": True, "expired": False
        }).encode())

    monkeypatch.setattr(subprocess, "run", run)
    assert sub2api_cli.push_session(args, reader)["configured"]
    payload = json.loads(captured["input"])
    assert payload["headers"]["authorization"].startswith("Bearer ")
    assert payload["headers"]["chatgpt-account-id"] == "codex-acct"
    # The Excel-plugin client identity still rides along, so the backend accepts it.
    assert payload["headers"]["x-openai-internal-basispoints-client-product"] == "basispoints-excel-plugin"


def test_control_plane_never_uses_proxy_or_redirects(monkeypatch):
    monkeypatch.setenv("EXCEL_SUB2API_ADMIN_KEY", ADMIN_KEY)
    monkeypatch.delenv("EXCEL_SUB2API_ADMIN_KEY_FILE", raising=False)
    actual_client = httpx.Client
    seen = []

    def record(request):
        seen.append(request)
        return httpx.Response(200, json={"configured": True, "expired": False})

    def factory(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return actual_client(transport=httpx.MockTransport(record), **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)
    sub2api_cli.control_request("session-status", 8000)
    assert str(seen[0].url) == "http://127.0.0.1:8000/admin/session"


def test_existing_launcher_dispatches_sub2api_without_starting_codex(monkeypatch):
    monkeypatch.setattr(sub2api_cli, "main", lambda argv: 17 if argv == ["--help"] else 1)
    assert cli.main(["sub2api", "--help"]) == 17


@pytest.mark.parametrize("stream", [False, True])
def test_gateway_preserves_tool_calls_and_usage(stream):
    from test_stream_completion import SOURCE_BODY, completed, created, officejs_call, parse
    chunks = officejs_call(0, "pwd")
    native_item = parse(chunks[-1])[0][1]["item"]
    raw = b"".join([created(), *chunks, completed([native_item])])
    harness = Harness(lambda _: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=raw
    ))
    harness.import_session()
    response = harness.request("POST", "/v1/responses",
                               json={**SOURCE_BODY, "input": "show current directory", "stream": stream})
    assert response.status_code == 200
    if stream:
        payload = next(p["response"] for name, p in parse(response.content) if name == "response.completed")
    else:
        payload = response.json()
    call = next(item for item in payload["output"] if item["type"] == "function_call")
    assert call["name"] == "shell_command"
    assert json.loads(call["arguments"]) == {"command": "pwd"}
    assert payload["usage"] == {"input_tokens": 5, "output_tokens": 7}


def test_session_expiring_after_import_stops_before_upstream(monkeypatch):
    harness = Harness()
    now = time.time()
    harness.import_session(headers=session_headers(now + 600))
    monkeypatch.setattr(excel_upstream.time, "time", lambda: now + 601)
    response = harness.request("POST", "/responses", json={"model": excel_upstream.MODEL_ID, "input": "hi"})
    assert response.status_code == 401
    assert not harness.requests

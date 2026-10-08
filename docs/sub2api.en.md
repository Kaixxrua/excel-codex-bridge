# Optional SUB2API sidecar

[简体中文 / complete setup guide](sub2api.md)

## Native route in 0.6

Compose and `excel-sub2api serve` now default to `EXCEL_BRIDGE_ROUTE=codex`. Select `--route excel`
or set `EXCEL_BRIDGE_ROUTE=excel` in Compose's `.env` for legacy BPS mode.

Native mode connects SUB2API to `chatgpt.com/backend-api/codex/responses`. Use the current client to
`push-session --login codex`; native mode rejects Excel sessions and old imports without a source.
Only explicit SSH sync transfers credentials. The server does not scan local credentials, sign in or
refresh tokens. Use native names returned by `/v1/models`, without `-excel`, and enable OpenAI passthrough
in SUB2API. Catalog requests require an imported session; health checks only establish liveness.
No sidecar inference retries or route fallback occur; SUB2API's own retries must be counted separately.
Start a new conversation after switching routes. The Excel-specific behavior below applies to legacy mode.

This is a private OpenAI-compatible upstream, **not** a SUB2API core patch or an Office add-in.
Its API and admin keys and explicit session-transfer boundary remain unchanged.

```text
Responses client -> SUB2API -> excel-sub2api:8000 -> selected Codex / Excel upstream
                                     ^
Your computer (Codex or Excel sign-in) -> SSH -> docker exec -> loopback admin API -> memory
```

## Explicit credential-transfer boundary

Only `push-session` exports the sign-in you choose, and only to the SSH server you name.
Unlike local mode, the server administrator can access that session and your requests.
Use a server you control, your own account, and comply with applicable service terms.
This does not bypass subscription limits. One session per instance; not a multi-account pool
or a multi-tenant isolation system. Sessions are memory-only and disappear on restart.

API and admin keys are separate. The admin endpoint also requires a real loopback peer.
Forwarded headers are not trusted. Do not reverse-proxy or HTTP-tunnel `/admin/session`.
Use SSH + `docker exec` instead; normal SSH host-key verification stays enabled.
Tokens travel on stdin, never in process arguments, temporary files or logs.

## Deploy on your server

From the repository root (Docker Compose and Python 3.10+ required):

```sh
python3 -m venv .venv
.venv/bin/pip install .
.venv/bin/excel-sub2api init-secrets packaging/sub2api/secrets
sudo chown 10001:10001 packaging/sub2api/secrets/api-key packaging/sub2api/secrets/admin-key
cd packaging/sub2api
cp .env.example .env
# Set SUB2API_NETWORK to the existing SUB2API Docker network name.
docker compose config --quiet
docker compose up -d --build
docker exec excel-sub2api excel-sub2api session-status
```

The container runs as UID/GID 10001, with a read-only filesystem, no added capabilities,
and **no published host ports**. File secrets retain restricted source permissions; the
`chown` step makes them readable by the non-root container on Linux. Keep the secret
directory private (0700) and the files private (0600), out of version control.

Add an **OpenAI / API Key** account in SUB2API:

- Base URL: `http://excel-sub2api:8000/v1`.
- API key: contents of `packaging/sub2api/secrets/api-key`, not the admin key.
- Enable OpenAI passthrough (`extra.openai_passthrough: true`).
- Use the models returned by `/v1/models`; preserve identity model mapping. Only Excel mode uses `-excel` aliases.
- Keep concurrency, group permissions and quotas in SUB2API. Clients use SUB2API-issued keys.

If your SUB2API version blocks private upstream addresses, narrowly allow this host/port
according to that version's documentation. Do **not** disable SSRF protection globally.

## Sync the session (Codex or Excel sign-in)

Run `codex login` locally for the default native route. The legacy Excel route can also use
Excel's ChatGPT pane. Verify your SSH host fingerprint interactively first. Then use the
installed Python entrypoint, or `excel-codex.exe sub2api ...` / `excel-codex.cmd sub2api ...` from
the existing distribution:

```sh
excel-sub2api push-session --ssh operator@your-vps --login codex
# For Docker via non-interactive sudo, resync every minute until Ctrl+C:
excel-sub2api push-session --ssh operator@your-vps --login codex --sudo --watch 60
# Only when the server explicitly uses --route excel:
excel-sub2api push-session --ssh operator@your-vps --login excel
```

`--login auto` (default) sends Codex's sign-in, else the Excel add-in's; `--login codex` / `excel`
force one, and `EXCEL_BRIDGE_LOGIN` works too. **This sends the sign-in you choose to the server** —
with `codex`/`auto`, Codex's token leaves this machine for the server you name; use one you trust.

Optional flags: `--ssh-port`, `--identity-file`, `--container`, `--webview-dir`,
`--login <auto|codex|excel>`. Use an SSH config alias for IPv6 hosts. Sync does not sign in or renew
a token itself; run `codex login` (Codex's) or refresh the Excel pane when required. Watch mode
retries failures and restores the session after a sidecar restart without saving it to disk.

## Protocol and operations

Authenticated `/v1/models` and `/v1/responses` (also available without `/v1`) reuse the
bridge's streaming, non-streaming, tool-call and image adaptation. `/healthz` is liveness
only. No Chat Completions, Messages, `/responses/compact`, WebSocket or dashboard.
Request/decompressed bodies are limited to 256 MiB; session imports to 64 KiB.
Do not run multiple Uvicorn workers: the session is process-local.

```sh
docker exec excel-sub2api excel-sub2api session-status
docker exec excel-sub2api excel-sub2api clear-session
```

For non-Docker usage set `EXCEL_SUB2API_API_KEY_FILE` and `EXCEL_SUB2API_ADMIN_KEY_FILE`
and run `excel-sub2api serve --host 127.0.0.1`. Environment values without `_FILE` are
also supported, but cannot be combined with their file counterparts.
`EXCEL_BRIDGE_PROXY` configures optional outbound OpenAI access, not control-plane access.

Run `python -m pytest -q` for regression tests. Tests use mock upstreams and synthetic
sessions, not real credentials or subscription quota. This adapter uses the repository's
Unlicense and integrates over HTTP; no SUB2API implementation is copied or distributed.

For repeatable container checks (automatically cleaned-up isolated network/container):

```sh
docker build -f packaging/sub2api/Dockerfile -t excel-sub2api:test .
python tests/e2e/sub2api_smoke.py --image excel-sub2api:test
python tests/e2e/sub2api_smoke.py --image excel-sub2api:test --route excel
```

# excel-codex-bridge

[简体中文](README.md) | **English**

> **Unofficial.** Not affiliated with, endorsed by, or supported by OpenAI or Microsoft.
> Read [Risks and disclaimer](#risks-and-disclaimer) before using it.

Run the [Codex CLI](https://github.com/openai/codex) on the ChatGPT session that the
**ChatGPT add-in for Excel** already keeps on *your own* computer. One command (or a
double-click) starts a local bridge and Codex together; quitting Codex stops the bridge.
Nothing logs in on your behalf, nothing listens beyond loopback, and the session stays in
memory and only goes to OpenAI.

## How it works

```
Codex CLI ──(Responses API, 127.0.0.1)──▶ excel-codex-bridge ──(HTTPS)──▶ bps.openai.com
                                                 ▲                      (ChatGPT for Excel backend)
                        reads the add-in's locally cached sign-in, read-only, never persisted
```

- **No login of its own.** No OAuth, no passwords. You sign in inside Excel's ChatGPT pane;
  the bridge only reads the session the add-in caches in its local WebView2 storage
  (`bps_auth_tokens`).
- **The token stays in memory.** It is never written to disk or uploaded, and is only sent to
  `bps.openai.com`. The log holds request lines and error types, never prompts or tokens.
- **Local only.** Loopback addresses only. Requests from non-loopback peers, with a non-loopback
  `Host` (DNS-rebinding guard), or with any `Origin` header (browsers) are refused. `serve`
  refuses to bind `0.0.0.0`.
- **Tool calls.** The Excel backend rejects client-defined tools, so the bridge describes
  Codex's tools (shell, apply_patch, …) in the prompt; the model calls them through the
  backend's native `run_officejs`, and the bridge turns those back into Codex tool calls.
- **Your Codex config is left alone.** The launcher passes the provider and model catalog as
  `-c` overrides, so `~/.codex/config.toml` is untouched and plain `codex` keeps working as before.

## Requirements

- Windows 10/11 with **Microsoft 365 desktop Excel** and the **ChatGPT** add-in (publisher
  OpenAI) installed and signed in at least once; your ChatGPT plan must include the add-in.
- Python 3.10+ ([python.org](https://www.python.org/downloads/); tick *Add python.exe to PATH*).
- Codex CLI: `npm install -g @openai/codex`.
- Network access to `bps.openai.com` (see [Proxy](#proxy)).

## Quick start (Windows)

1. Download or clone this repository anywhere, e.g. `D:\tools\excel-codex-bridge`.
2. In Excel open Home → Add-ins → **ChatGPT** and make sure the pane is signed in. You can
   close Excel afterwards.
3. Open cmd or PowerShell in your project folder and run:

   ```
   D:\tools\excel-codex-bridge\excel-codex.cmd
   ```

   The first run creates `.venv` next to the script and installs dependencies; later runs
   start instantly. Double-clicking `excel-codex.cmd` also works; Codex then starts in your
   user folder. Add the repository folder to `PATH` to type `excel-codex` anywhere.

Common invocations (everything after `--` goes to Codex unchanged):

```
excel-codex                                   interactive Codex, default gpt-5.6-sol-excel
excel-codex --model gpt-5.6-terra-excel       pick another model
excel-codex -- -c model_reasoning_effort=high set reasoning effort
excel-codex -- exec "add a table of contents to the README"
excel-codex -- resume --last                  resume the last session
excel-codex status                            is the session usable, and when does it expire
```

Launcher options: `--model`, `--proxy`, `--port`, `--codex <path>`, `--webview-dir <dir>`,
`--skip-session-check`.

## Models

| Choose in Codex | Upstream model | Context |
| --- | --- | --- |
| `gpt-5.6-sol-excel` (default) | `gpt-5.6-sol` | 272k |
| `gpt-5.6-terra-excel` | `gpt-5.6-terra` | 272k |
| `gpt-5.6-luna-excel` | `gpt-5.6-luna` | 200k |

Reasoning effort `low` / `medium` / `high` / `xhigh`, default `medium`.

## Proxy

For the bridge's outbound connection to `bps.openai.com`:

- `--proxy http://127.0.0.1:7890` (`socks5h://…` works too), or the `EXCEL_BRIDGE_PROXY` variable;
- otherwise `HTTPS_PROXY` and the system proxy are used.

TLS certificate verification is always on. Codex reaches the bridge on `127.0.0.1`, which the
launcher adds to `NO_PROXY`.

## Codex IDE extension / desktop app

These clients cannot take `-c`, so run the bridge on its own and add a config snippet:

```
excel-codex serve          keep this window open; listens on 127.0.0.1:8765
excel-codex print-config   prints the lines to add to ~/.codex/config.toml
```

Remove those lines from `config.toml` to go back.

## macOS / WSL (experimental)

- **macOS**: `./excel-codex.sh` reads the WebKit local storage in Excel's sandbox.
- **WSL**: Excel lives on the Windows side, so point the bridge at its data folder:

  ```
  ./excel-codex.sh --webview-dir /mnt/c/Users/<you>/AppData/Local/Microsoft/Office
  ```

## Session expiry

The add-in's token lasts about 10 days. The bridge checks it before every request and re-reads
the local cache when it has expired or is about to, so opening the ChatGPT pane in Excel once
is enough; **no restart** of the bridge or Codex is needed. `excel-codex status` shows the time left.

## Limitations

- Text input only; no images.
- No parallel tool calls; one tool at a time.
- Responses API only; there is no `/responses/compact` endpoint.
- The Excel backend adds a fixed prefix of about 22k tokens to every request (mostly served from
  cache); usage counts against your ChatGPT plan.
- It relies on a private backend of the Excel add-in and may break whenever OpenAI changes it.

## Risks and disclaimer

- This is an **unofficial** project, not endorsed by OpenAI or Microsoft. Using the add-in's
  backend outside Excel may violate OpenAI's terms of use and could get your account limited or
  banned. **Use at your own risk.**
- Use it only with **your own account on your own computer**. Do not expose the bridge to a
  network, share it with others, or use it as a relay or resale service. The project deliberately
  does not support that: no multi-user mode, no API key management, no non-loopback listening.
- The tool reads a sign-in credential. Reading happens locally; the credential is only held in
  memory and only sent to `bps.openai.com`. Review the code before using it.

## Environment variables

| Variable | Purpose |
| --- | --- |
| `EXCEL_BRIDGE_PROXY` | Outbound proxy (same as `--proxy`) |
| `EXCEL_BRIDGE_HOME` | State folder for the model catalog JSON and `bridge.log`. Default `%LOCALAPPDATA%\excel-codex-bridge` or `~/.excel-codex-bridge` |
| `GHCP_EXCEL_WEBVIEW2_DATA_DIR` | Windows WebView2 data root (same as `--webview-dir`) |
| `GHCP_EXCEL_WEBKIT_WEBSITE_DATA_DIR` | macOS WebKit data folder |
| `GHCP_EXCEL_RESPONSES_URL` | Upstream URL (for testing) |
| `GHCP_EXCEL_UPSTREAM_MODEL` | Force the upstream model name |

The `GHCP_EXCEL_*` names match ghcp_proxy, so existing setups carry over.

## Development

```
python -m venv .venv
.venv/bin/pip install -r requirements.txt pytest     # Windows: .venv\Scripts\pip
PYTHONPATH=src .venv/bin/python -m pytest
```

## Credits and license

The Excel session reader and the Basispoints protocol adapter are extracted from
[Nonary/ghcp_proxy](https://github.com/Nonary/ghcp_proxy) (Unlicense, based on commit `ad23ce2`;
original license in [UPSTREAM-LICENSE](UPSTREAM-LICENSE)). This project is released under the
[Unlicense](LICENSE) as well.

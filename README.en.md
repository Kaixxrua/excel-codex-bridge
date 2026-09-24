# excel-codex-bridge

[简体中文](README.md) | **English**

> **Unofficial.** Not affiliated with, endorsed by, or supported by OpenAI or Microsoft.
> Read [Risks and disclaimer](#risks-and-disclaimer) before using it.

Run the [Codex CLI](https://github.com/openai/codex) and the Codex desktop app on the ChatGPT
session that the **ChatGPT add-in for Excel** keeps on *your own* computer. One command (or a
double-click) starts a local bridge and Codex together; quitting stops the bridge. When a sign-in
is needed, it opens Excel's ChatGPT pane for you and the add-in signs in itself. Nothing listens
beyond loopback, and the session stays in memory and only goes to OpenAI.

## How it works

```
Codex CLI ──(Responses API, 127.0.0.1)──▶ excel-codex-bridge ──(HTTPS)──▶ bps.openai.com
                                                 ▲                      (ChatGPT for Excel backend)
                        reads the add-in's locally cached sign-in, read-only, never persisted
```

- **No login of its own.** No OAuth, no passwords. You sign in inside Excel's ChatGPT pane
  (which the tool [opens for you](#automatic-sign-in-windows) when needed); the bridge only reads
  the session the add-in caches in its local WebView2 storage (`bps_auth_tokens`).
- **The token stays in memory.** It is never written to disk or uploaded, and is only sent to
  `bps.openai.com`. The log holds request lines and error types, never prompts or tokens.
- **Local only.** Loopback addresses only. Requests from non-loopback peers, with a non-loopback
  `Host` (DNS-rebinding guard), or with any `Origin` header (browsers) are refused. `serve`
  refuses to bind `0.0.0.0`.
- **Tool calls.** The Excel backend rejects client-defined tools, so the bridge describes
  Codex's tools (shell, apply_patch, …) in the prompt; the model calls them through the
  backend's native `run_officejs`, and the bridge turns those back into Codex tool calls.
- **Pictures.** The backend only takes pictures as links OpenAI can fetch. By default the bridge
  keeps them in memory on your computer and lets OpenAI fetch them briefly through a temporary
  tunnel; see [Pictures](#pictures).
- **Your Codex config is left alone.** The CLI launcher passes the provider and model catalog as
  `-c` overrides, so `~/.codex/config.toml` is untouched. Only [desktop mode](#codex-desktop-app--ide-extension)
  edits it, restores it exactly when its window closes, and keeps a backup.

## Requirements

- Windows 10/11 with **Microsoft 365 desktop Excel** and the **ChatGPT** add-in (publisher
  OpenAI). No need to sign in first: the first run opens Excel and walks you through it. If the
  add-in is not installed, Excel usually offers to trust and install it; otherwise add it from
  Home → Add-ins. Your ChatGPT plan must include the add-in.
  On a Mac, see [macOS](#macos--wsl-experimental).
- Codex CLI: `npm install -g @openai/codex`.
- Python 3.10+ only when running from source ([python.org](https://www.python.org/downloads/);
  tick *Add python.exe to PATH*); the no-install build does not need it.
- Network access to `bps.openai.com` (see [Proxy](#proxy)).

## Quick start (Windows)

**Option 1: no-install build (recommended)**

1. Download `excel-codex-bridge-<version>-windows-x64.zip` from
   [Releases](https://github.com/Kaixxrua/excel-codex-bridge/releases/latest) and unzip it anywhere,
   e.g. `D:\tools\excel-codex-bridge`.
2. Double-click `excel-codex.exe` to open Codex in your user folder. To work on a project, run
   `D:\tools\excel-codex-bridge\excel-codex.exe` from a terminal in that folder, or add the folder
   to `PATH` and type `excel-codex`.

The exe is not code-signed; if SmartScreen stops the first run, choose "More info → Run anyway".

On the first run there is no session yet, so Excel opens with the ChatGPT pane: sign in there,
Excel closes by itself and Codex starts. See [Automatic sign-in](#automatic-sign-in-windows).

**Option 2: from source**

Clone this repository anywhere and run its `excel-codex.cmd` from your project folder (or
double-click it). The first run creates `.venv` next to the script and installs dependencies;
later runs start instantly.

Common invocations (everything after `--` goes to Codex unchanged):

```
excel-codex                                   interactive Codex, default gpt-5.6-sol-excel
excel-codex --model gpt-5.6-terra-excel       pick another model
excel-codex -- -c model_reasoning_effort=high set reasoning effort
excel-codex -- exec "add a table of contents to the README"
excel-codex -- resume --last                  resume the last session
excel-codex status                            is the session usable, and when does it expire
excel-codex login                             open Excel's ChatGPT pane to sign in or refresh
excel-codex desktop                           route the Codex desktop app / IDE extension here
```

Launcher options: `--model`, `--proxy`, `--port`, `--codex <path>`, `--webview-dir <dir>`,
`--skip-session-check`, `--no-auto-signin`.

## Models

| Choose in Codex | Upstream model | Context |
| --- | --- | --- |
| `gpt-5.6-sol-excel` (default) | `gpt-5.6-sol` | 272k |
| `gpt-5.6-terra-excel` | `gpt-5.6-terra` | 272k |
| `gpt-5.6-luna-excel` | `gpt-5.6-luna` | 200k |
| `gpt-6-astra-excel` (experimental) | `gpt-6-astra` | treated as 272k |

Reasoning effort `low` / `medium` / `high` / `xhigh`, default `medium`.

`gpt-6-astra-excel` is not yet confirmed on the Excel backend; a model error means it is not
available to your account there yet. To try any other upstream model, set
`GHCP_EXCEL_UPSTREAM_MODEL=<upstream name>`, which sends every request to that model.

## Proxy

For the bridge's outbound connection to `bps.openai.com`:

- `--proxy http://127.0.0.1:7890` (`socks5h://…` works too), or the `EXCEL_BRIDGE_PROXY` variable;
- otherwise `HTTPS_PROXY` and the system proxy are used.

TLS certificate verification is always on. Codex reaches the bridge on `127.0.0.1`, which the
launcher adds to `NO_PROXY`.

## Automatic sign-in (Windows)

The tool never signs in itself; it opens the official ChatGPT pane in Excel for you:

1. With no usable session, it writes a small workbook
   (`%LOCALAPPDATA%\excel-codex-bridge\excel-codex-sign-in.xlsx`) that embeds the ChatGPT add-in
   (Marketplace asset `WA200010215`) and opens it in Excel, which shows the pane.
2. The first time, Excel asks you to trust / install the add-in; then sign in in the pane. If no
   pane appears, click Home → Add-ins → ChatGPT.
3. Once the new session shows up, the workbook is closed again, and Excel too if the tool started
   it and nothing else is open.

When less than 24 hours are left, a running bridge opens Excel minimized in the background; a
signed-in pane usually refreshes on its own, and Excel closes again. `excel-codex login` does this
on demand (`--force` opens the pane even when the session is fine). To keep the tool away from
Excel, pass `--no-auto-signin` or set `EXCEL_BRIDGE_AUTO_SIGNIN=0`.

> Opening the pane relies on Office's "open an add-in with a document" feature. It is new in
> v0.2.1 and not yet verified on every Office build; some builds or organization policies may not
> honor it. Then open the pane by hand (step 2) and the rest still happens automatically.

## Codex desktop app / IDE extension

These clients cannot take `-c` and read only `~/.codex/config.toml`, which the tool can point at
the bridge for as long as you need it:

1. Double-click **`excel-codex-desktop.cmd`** from the release zip (or run `excel-codex desktop`).
   It checks the session (signing in if needed), points `config.toml` at the bridge and runs the
   bridge on `127.0.0.1:8765`.
2. **Restart the Codex desktop app** (or reload the IDE window); the model list shows the
   `*-excel` models.
3. Keep that window open while you work (minimizing is fine). **Closing it or pressing Ctrl+C
   restores `config.toml` exactly.**

Details:

- The original file is saved as `config.toml.before-excel-codex`. Your own `model`,
  `model_provider` and similar lines are only commented out and come back byte for byte.
- `config.toml` is shared by every Codex client, so plain `codex` in a terminal also uses the
  bridge while the window is open.
- To keep it on: `excel-codex desktop --keep-config`, and later `excel-codex desktop --off`
  (also the fix if the window was killed before it could restore the config).
- Other model: `excel-codex desktop --model gpt-5.6-terra-excel`; other port: `--port`.

Fully manual alternative: run `excel-codex serve` and add the output of `excel-codex print-config`
to `config.toml`; remove those lines to go back.

## macOS / WSL (experimental)

**macOS, no install** (reads the WebKit local storage in Mac Excel's sandbox; no Windows PC needed):

1. In Mac Excel, click Home → Add-ins → ChatGPT and sign in once in the pane. There is no automatic
   sign-in on the Mac; the session lasts about 10 days, then open the pane again. The bridge does
   not need a restart.
2. Download `excel-codex-bridge-<version>-macos-arm64.tar.gz` (Intel Macs: `macos-x64`) from
   [Releases](https://github.com/Kaixxrua/excel-codex-bridge/releases/latest) and double-click it to extract.
3. The build is not signed by Apple. If you downloaded it with a browser, clear the quarantine flag once
   in Terminal, or macOS will say the developer cannot be verified:

   ```
   xattr -dr com.apple.quarantine ~/Downloads/excel-codex-bridge-<version>-macos-arm64
   ```

   (Downloads made with `curl` carry no quarantine flag; the release notes have the command.)
4. Then use it as on Windows:
   - Codex CLI: run `<folder>/excel-codex` from your project folder, with the same arguments; or add
     the folder to `PATH`.
   - Codex desktop app: double-click `excel-codex-desktop.command`, then quit the desktop app with
     `Cmd+Q` and open it again. Keep the Terminal window open; closing it or pressing Ctrl+C restores
     `config.toml` exactly.

The first time the session is read, macOS may ask whether Terminal may access data from other apps;
allow it. To run from source instead, use `./excel-codex.sh` (Python 3.10+), and
`./excel-codex.sh desktop` for desktop mode.

**WSL**: Excel lives on the Windows side, so point the bridge at its data folder:

  ```
  ./excel-codex.sh --webview-dir /mnt/c/Users/<you>/AppData/Local/Microsoft/Office
  ```

## Session expiry

The add-in's token lasts about 10 days. The bridge checks it before every request and re-reads
the local cache when it has expired or is about to; on Windows it also
[refreshes it through Excel](#automatic-sign-in-windows) when less than 24 hours are left. **No
restart** of the bridge or Codex is needed. `excel-codex status` shows the time left.

## Pictures

Screenshots pasted into Codex, `codex -i picture.png` and the model's `view_image` all work. The
Excel backend only takes pictures as links that OpenAI can fetch, never inline, so by default the
bridge passes them on like this:

- pictures are kept only in the bridge's **memory**: nothing is written to disk, and they are gone
  when the bridge stops;
- with the first picture, the bridge opens a temporary Cloudflare tunnel
  (`https://<random-words>.trycloudflare.com`, no account needed) with the
  [cloudflared](https://github.com/cloudflare/cloudflared) that ships in the release packages;
- each picture's link has a random, unguessable name and can be fetched only **for 5 minutes after
  a request containing it is sent**; nothing else is reachable through the tunnel.

Pictures travel to OpenAI through Cloudflare's network and are not uploaded anywhere else. Opening
the tunnel takes a few seconds. The tunnel does not use `--proxy`, so it cannot work on a network
that blocks Cloudflare tunnels (outbound port 7844). If the tunnel cannot open or OpenAI cannot
fetch a picture, the bridge replaces it with a short note and sends the request anyway; the reason
is in the window or `bridge.log`.

`excel-codex image-host` shows the current setting, and switches it (restart `excel-codex`
afterwards; in desktop mode, the Codex app as well):

| Command | Effect |
| --- | --- |
| `excel-codex image-host local` | Default: pass pictures on from this computer (as above) |
| `excel-codex image-host off` | Turn pictures off; the model gets text only |
| `excel-codex image-host set <url> <token>` | Upload to your own image host (below) |

When running from source or in WSL, install cloudflared yourself (Windows:
`winget install Cloudflare.cloudflared`, macOS: `brew install cloudflared`) or put it in the
state folder's `bin/`. Without cloudflared, pictures are off.

**Your own image host (optional).** If the tunnel cannot open where you are, run
`python -m excel_codex_bridge.image_host` on a public server (set `IMAGE_HOST_PUBLIC_URL` and
`IMAGE_HOST_TOKENS`, behind an https reverse proxy) and point the bridge at it with
`image-host set`. Pictures are then kept on that server for 24 hours and anyone with a link can
open them, which is less private than the default.

## Limitations

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
| `EXCEL_BRIDGE_HOME` | State folder for the model catalog JSON, `bridge.log`, the sign-in workbook, and `tool-calls.sqlite3`, which lets earlier tool calls replay exactly after a restart (kept 60 days). Default `%LOCALAPPDATA%\excel-codex-bridge` or `~/.excel-codex-bridge` |
| `EXCEL_BRIDGE_IMAGE_HOST` | How pictures are passed on: `local`, `off` or your image host's URL; overrides `image-host` |
| `EXCEL_BRIDGE_IMAGE_TOKEN` | Upload token for your own image host |
| `EXCEL_BRIDGE_CLOUDFLARED` | Path to the cloudflared binary |
| `EXCEL_BRIDGE_AUTO_SIGNIN` | `0` keeps the tool from opening Excel (same as `--no-auto-signin`) |
| `CODEX_HOME` | Codex config folder whose `config.toml` `desktop` edits. Default `~/.codex` |
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
[Unlicense](LICENSE) as well. The release packages include Cloudflare's
[cloudflared](https://github.com/cloudflare/cloudflared) to pass pictures on; it is released under
the Apache License 2.0 (`CLOUDFLARED-LICENSE` in the package).

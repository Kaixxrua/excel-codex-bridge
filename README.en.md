# excel-codex-bridge

[简体中文](README.md) | **English**

> **Unofficial.** Not affiliated with, endorsed by, or supported by OpenAI or Microsoft.
> Read [Risks and disclaimer](#risks-and-disclaimer) before using it.

> **Optional SUB2API plugin:** run Excel as a private sidecar upstream without modifying
> SUB2API. See [Docker deployment and SSH session sync](docs/sub2api.en.md).
> This explicitly opted-in mode transfers your session to your trusted server.
> The local-only / OpenAI-only guarantees below describe the default local mode, not this remote mode.

Run the [Codex CLI](https://github.com/openai/codex) and the Codex desktop app on a ChatGPT
sign-in already on *your own* computer. One command (or a double-click) starts a local bridge and
Codex together; quitting stops the bridge. By default it uses Codex's own sign-in
(`codex login`), so Excel need not be installed; if the backend refuses it, it falls back to the
session the **ChatGPT add-in for Excel** caches, opening Excel's ChatGPT pane for you when a
sign-in is needed. See [Sign-in](#sign-in). Nothing listens beyond loopback, and the session
stays in memory and only goes to OpenAI.

## How it works

```
Codex CLI ──(Responses API, 127.0.0.1)──▶ excel-codex-bridge ──(HTTPS)──▶ bps.openai.com
                                                 ▲                      (ChatGPT for Excel backend)
              reads a ChatGPT sign-in already here (Codex's own, or the add-in's), read-only, never persisted
```

- **No login of its own.** No OAuth, no passwords, and it never refreshes or writes a token. It
  only reads a ChatGPT sign-in already on this machine — Codex's own first (`codex login`, kept in
  `~/.codex/auth.json`), then the session the Excel add-in caches in its WebView2 storage
  (`bps_auth_tokens`). With the add-in's, you sign in inside its ChatGPT pane (which the tool
  [opens for you](#automatic-sign-in-windows) when needed). Which one it takes, and how it falls
  back, is under [Sign-in](#sign-in).
- **The token stays in memory.** It is never written to disk or uploaded, and is only sent to
  `bps.openai.com`. The log holds request lines and error types, never prompts or tokens.
- **Local only.** Loopback addresses only. Requests from non-loopback peers, with a non-loopback
  `Host` (DNS-rebinding guard), or with any `Origin` header (browsers) are refused. `serve`
  refuses to bind `0.0.0.0`.
- **Tool calls.** The Excel backend rejects client-defined tools, so the bridge describes
  Codex's tools (shell, apply_patch, …) in the prompt; the model calls them through the
  backend's native `run_officejs`, and the bridge turns those back into Codex tool calls.
  Calls that do not depend on each other (reading several files, independent commands) come in
  one go and Codex runs them at the same time; file edits still happen one after another.
- **Several sessions at once.** Codex windows, desktop conversations and subagents can share one
  bridge and work at the same time without queuing.
- **Pictures.** Pictures go to OpenAI only, like the rest of the request. Where the backend will
  not take them inline, the bridge uploads them to OpenAI the way the add-in's Upload file button
  does; see [Pictures](#pictures).
- **Image generation.** Codex's own image tool is answered by the endpoint the Excel add-in draws
  pictures with (gpt-image-2), on the same session; see [Image generation](#image-generation).
- **Your Codex config is left alone.** The CLI launcher passes the provider and model catalog as
  `-c` overrides, so `~/.codex/config.toml` is untouched. Only [desktop mode](#codex-desktop-app--ide-extension)
  edits it, restores it exactly when its window closes, and keeps a backup.

## Requirements

- Codex CLI: `npm install -g @openai/codex`.
- One ChatGPT sign-in already on this machine, either (see [Sign-in](#sign-in)):
  - **Codex's own sign-in** (the default, no Excel needed): run `codex login` once with ChatGPT,
    on any OS.
  - **The Excel add-in's session** (the fallback): Windows 10/11 with **Microsoft 365 desktop
    Excel** and the **ChatGPT** add-in (publisher OpenAI); on a Mac see
    [macOS](#macos--wsl-experimental). No need to sign in first — the tool opens Excel and walks
    you through it when needed. If the add-in is not installed, Excel usually offers to trust and
    install it; otherwise add it from Home → Add-ins. Your ChatGPT plan must include the add-in.
- Python 3.10+ only when running from source ([python.org](https://www.python.org/downloads/);
  tick *Add python.exe to PATH*); the no-install build does not need it.
- Network access to `bps.openai.com` (see [Proxy](#proxy)).

## Sign-in

The bridge needs a ChatGPT sign-in already on this machine. Two are possible, chosen in order by
default:

1. **Codex's own sign-in** (`codex login`, kept in `$CODEX_HOME/auth.json`, default
   `~/.codex/auth.json`). With this one Excel need not be installed, and it works on any OS.
2. **The session the Excel add-in caches** (the fallback). Used when Codex is not signed in with
   ChatGPT, its sign-in has expired, or the backend refuses it; a sign-in then
   [opens Excel's pane](#automatic-sign-in-windows).

Pick one with `--login` or the `EXCEL_BRIDGE_LOGIN` environment variable:

| Value | Meaning |
| --- | --- |
| `auto` (default) | Try Codex's sign-in, then the Excel add-in's |
| `codex` | Only Codex's sign-in (Excel is never touched) |
| `excel` | Only the Excel add-in's session (the original behaviour) |

- **When it falls back** (in `auto` only): Codex is not signed in / uses an API key / its sign-in
  has expired or expires within 5 minutes / the backend answers 401 or 403. After a 401/403 it
  retries once on the Excel session and then stays on Excel until `auth.json` changes (e.g. you run
  `codex login` again), when it tries Codex once more.
- `EXCEL_BRIDGE_CODEX_AUTH` points at a different `auth.json` (otherwise `$CODEX_HOME`, then
  `~/.codex`).
- When a sign-in is expired or unusable, `excel-codex status` says which one is in use, when it
  expires, and how to renew it (`codex login` for Codex's; `excel-codex login` for Excel's).
- The bridge never refreshes or writes a token; when Codex's expires, run `codex login` yourself
  (in `auto` it falls back to Excel meanwhile).
- [SUB2API remote sync](docs/sub2api.en.md) only ever sends the Excel add-in's session; Codex's
  sign-in never leaves this machine.

> **Verified** (2026-09-25): tested with Codex CLI 0.156.1 — after a fresh `codex login` the bps
> backend returns 200, so Codex's own sign-in works directly, with no Excel installed. Should a
> future backend change refuse it, `auto` still falls back to the Excel session.

This idea comes from [MIKUbiu/bps-local](https://github.com/MIKUbiu/bps-local); this project is a
separate implementation of it, see [Credits and license](#credits-and-license).

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
excel-codex status                            which sign-in is used, is it usable, when it expires
excel-codex --login codex                      use Codex's own sign-in only (never touch Excel)
excel-codex login                             open Excel's ChatGPT pane to sign in or refresh
excel-codex desktop                           route the Codex desktop app / IDE extension here
```

Launcher options: `--login <auto|codex|excel>`, `--model`, `--proxy`, `--port`, `--codex <path>`,
`--webview-dir <dir>`, `--skip-session-check`, `--no-auto-signin`.

## Models

| Choose in Codex | Upstream model | Context |
| --- | --- | --- |
| `gpt-5.6-sol-excel` (default) | `gpt-5.6-sol` | 272k |
| `gpt-5.6-terra-excel` | `gpt-5.6-terra` | 272k |
| `gpt-5.6-luna-excel` | `gpt-5.6-luna` | 200k |
| `gpt-6-astra-excel` | `gpt-6-astra` | treated as 272k |

Reasoning effort `low` / `medium` / `high` / `xhigh`, default `medium`.

To try any other upstream model, set `GHCP_EXCEL_UPSTREAM_MODEL=<upstream name>`, which sends
every request to that model.

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
2. **Fully quit and reopen the Codex desktop app** (or reload the IDE window); the model list
   shows the `*-excel` models. **Start a new conversation** with them: conversations started
   earlier keep the account they were started with.
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

**Error `The '…-excel' model is not supported when using Codex with a ChatGPT account`** (after
signing out of Codex it becomes `401 Unauthorized: Missing bearer or basic authentication` for
`api.openai.com/v1/responses`). The conversation's request did not go through the bridge; it went
straight to OpenAI, so signing out does not help. The desktop app reads `config.toml` when a
conversation starts to decide where its requests go, and the conversation keeps that choice. The
model list, though, is read only when the desktop app starts. So the list can still show the
`*-excel` models while the conversation is tied to OpenAI's own service. Common causes:

- The bridge window was closed (so `config.toml` was restored) but the desktop app was not
  restarted.
- The conversation was started before desktop mode was on; switching it to an Excel model does
  not help.
- A tool such as Cockpit Tools switched Codex back to a ChatGPT account.

Fix: open `excel-codex-desktop.cmd`, fully quit the desktop app (File → Quit, or quit from the
tray; after closing the window it may still run in the background) and reopen it, then **start a
new conversation**. Each message then shows a line like `"POST /v1/responses HTTP/1.1" 200` in the
bridge window (0.4.2 and later); if nothing shows up, the request still bypasses the bridge.

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

Screenshots pasted into Codex, `codex -i picture.png` and the model's `view_image` all work, with
nothing to set up. The bridge passes them on like this:

- **Inline where it can.** Pictures go with the request the way Codex sends them (inline
  `data:` URLs), without a separate upload.
- **Uploaded only where inline is refused.** Where the Excel backend will not take a picture inline
  (at present, in user messages), the bridge uploads it the way the Upload file button in the Excel
  add-in does: on the same session, to OpenAI's attachments endpoint
  (`bps.openai.com/basispoints/api/attachments`), and the request names it by the file id that comes
  back. The bridge finds out where by itself: the first refusal switches that kind of place to
  uploads, and later pictures there are uploaded straight away.
- **Each picture once.** Later requests in the same run reuse its file id (per account, kept in
  memory only).
- If an upload fails or the backend still refuses, the bridge replaces the picture with a short note
  and sends the request anyway; the reason is in the window or in `bridge.log`.

Pictures go to `bps.openai.com` only, like the rest of the request: no third party, no tunnel, no
image host and nothing to configure. Uploads use the same network settings as requests (including
`--proxy`). OpenAI keeps uploaded pictures as it keeps files you upload in the add-in.

**The desktop app says "This model does not support image inputs".** The desktop app blocks the
picture itself; no request is sent. It decides from the model catalog, and it reads the catalog only
at startup:

- Versions 0.3.1 and earlier wrote a catalog that marked the Excel models as text-only. After
  upgrading, start desktop mode again (`excel-codex-desktop.cmd` or `excel-codex desktop`), then
  fully quit the desktop app (quit from the tray, or `Cmd+Q` on macOS) and open it again.
- When a tool such as Cockpit Tools manages Codex, that tool writes the model catalog, not this
  bridge. Turn on image input for the `*-excel` models in its model provider settings (Cockpit 1.3.57
  and earlier leave it off by default).

## Image generation

From 0.4.6, Codex's own image tool works: ask Codex to "draw …" or "change this picture to …".
The picture shows up in the conversation and is saved under `~/.codex/generated_images/`.

- Codex offers the tool only when the provider sets an `x-openai-actor-authorization` header. The
  launcher's `-c` overrides, desktop mode and `print-config` all set it (to `excel-codex-bridge`;
  the bridge does not use the value or send it anywhere). If you added this header by hand earlier,
  you can remove it after upgrading.
- The bridge sends the request to the endpoints the add-in draws with: a new picture to
  `bps.openai.com/basispoints/api/images/generations`, a change to `/images/edits`, on the same
  session and with the add-in's choices: gpt-image-2, PNG, sizes `auto`, `1024x1024`, `1536x1024`,
  `1024x1536` and `1280x720`.
- Transparent backgrounds are not available (the add-in does not offer them either). When the
  model asks for one it is told so, and can draw on a plain background instead.
- Pictures count against your ChatGPT plan. When the backend refuses, Codex shows the reason the
  backend gave.
- When a tool such as Cockpit Tools manages Codex, it writes the provider, so add
  `http_headers = { "x-openai-actor-authorization" = "excel-codex-bridge" }` to its provider
  settings yourself.

## Update check

From 0.4.4 on, the tool checks GitHub in the background for a newer release (at most every 12 hours)
and, if there is one, shows its version, what changed and the download link:

- In the desktop-mode and `serve` windows: after startup, and whenever a release comes out while the
  window stays open.
- When `excel-codex` starts Codex: after Codex exits. A window opened by double-click then waits for
  Enter before it closes.

The request goes only to `api.github.com`, carries nothing but the version in its User-Agent, and
uses the same proxy settings as the bridge. If GitHub cannot be reached, nothing is shown and nothing
else changes. Set `EXCEL_BRIDGE_UPDATE_CHECK=0` to turn it off.

To update, close any running bridge window and Codex, then extract the new release over the old folder
(or into a new one). Settings and state are not kept in the install folder, so nothing is lost. When
running from source, `git pull` is enough.

## Limitations

- Responses API only; there is no `/responses/compact` endpoint.
- The Excel backend adds a fixed prefix of about 22k tokens to every request (mostly served from
  cache); usage counts against your ChatGPT plan, and more sessions at once use it up faster.
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
| `EXCEL_BRIDGE_LOGIN` | Which sign-in to use: `auto` (default) / `codex` / `excel` (same as `--login`), see [Sign-in](#sign-in) |
| `EXCEL_BRIDGE_CODEX_AUTH` | Path to Codex's `auth.json`; default `$CODEX_HOME/auth.json`, then `~/.codex/auth.json` |
| `EXCEL_BRIDGE_PROXY` | Outbound proxy (same as `--proxy`) |
| `EXCEL_BRIDGE_HOME` | State folder for the model catalog JSON, `bridge.log`, the sign-in workbook, and `tool-calls.sqlite3`, which lets earlier tool calls replay exactly after a restart (kept 60 days). Default `%LOCALAPPDATA%\excel-codex-bridge` or `~/.excel-codex-bridge` |
| `EXCEL_BRIDGE_AUTO_SIGNIN` | `0` keeps the tool from opening Excel (same as `--no-auto-signin`) |
| `EXCEL_BRIDGE_UPDATE_CHECK` | `0` turns off the update check |
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
[Unlicense](LICENSE) as well.

**The idea of using Codex's own sign-in to reach the Excel backend (so Excel need not be
installed)** comes from [MIKUbiu/bps-local](https://github.com/MIKUbiu/bps-local) (Unlicense) —
thanks to MIKUbiu for it. This project is a separate implementation: it wires that sign-in in as an
optional source of the existing bridge and fallback, with our own code. bps-local's Basispoints
protocol code derives from [hloolx/codex2api](https://github.com/hloolx/codex2api) (MIT, ported via
[ranxi2001/sub2api](https://github.com/ranxi2001/sub2api)); our own such code comes from ghcp_proxy
above.

Thanks to the [LINUX DO](https://linux.do) community.

默认用 Codex 自己的登录，不必装 Excel · Uses Codex's own sign-in by default, no Excel needed

> **非官方项目**，与 OpenAI、Microsoft 无关联。使用加载项后端可能违反 OpenAI 服务条款，风险自负。
> **Unofficial.** Not affiliated with OpenAI or Microsoft. Using the add-in's backend may violate OpenAI's terms; use at your own risk.

## 变化

- **登录方式**：现在默认（`auto`）优先用 Codex 自己的 ChatGPT 登录（`codex login`，读 `~/.codex/auth.json`），
  这样**不必安装 Excel**。后端不接受它、Codex 未登录或登录过期时，自动回退到 Excel 加载项缓存的会话，
  和以前完全一样。用 `--login` 或 `EXCEL_BRIDGE_LOGIN` 可指定 `auto` / `codex`（只用 Codex）/ `excel`
  （只用 Excel，即老用户的原有行为）。
- 回退时机（仅 `auto`）：Codex 未登录 / 用的是 API key / 登录过期或将在 5 分钟内过期 / 后端返回 401、403。
  被拒后先用 Excel 会话重试一次，之后一直用 Excel，直到你重新 `codex login`（`auth.json` 变化）才再试 Codex。
- `excel-codex status` 现在会显示当前用的是哪一份登录、何时过期、如何续期。桥接从不刷新或写入任何 token。
- `EXCEL_BRIDGE_CODEX_AUTH` 可指向别的 `auth.json`。[SUB2API 远端同步](https://github.com/Kaixxrua/excel-codex-bridge/blob/v0.5.0/docs/sub2api.md)
  只会送 Excel 加载项的会话，Codex 的登录不会离开本机。
- **尚未验证**：Excel 后端是否接受 Codex CLI 自己的登录 token，还没在真实后端上测过。不接受时 `auto` 会自动
  回退到 Excel 会话；只想用经过验证的老链路，设 `--login excel`。遇到后端拒绝，请把 Codex 里显示的原因发到群里。
- 此想法来自 [MIKUbiu/bps-local](https://github.com/MIKUbiu/bps-local)（Unlicense），本项目是它的一个独立实现。感谢 MIKUbiu。

## 下载

- **Windows**：`excel-codex-bridge-0.5.0-windows-x64.zip`。解压后双击 `excel-codex.exe` 打开 Codex CLI，
  双击 `excel-codex-desktop.cmd` 给桌面版用。
- **macOS（Apple 芯片）**：`excel-codex-bridge-0.5.0-macos-arm64.tar.gz`
- **macOS（Intel）**：`excel-codex-bridge-0.5.0-macos-x64.tar.gz`
- **Linux / WSL 或从源码运行**：下载 Source code，使用 `excel-codex.sh`（需要 Python 3.10+）。
- **Linux / VPS 的 SUB2API 部署**：下载本版本 Source code，按 [部署文档](https://github.com/Kaixxrua/excel-codex-bridge/blob/v0.5.0/docs/sub2api.md) 构建 `packaging/sub2api/compose.yaml`。

macOS 推荐在终端用 `curl` 下载，这样不会被"无法验证开发者"拦下（Intel 芯片把 `arm64` 换成 `x64`）：

```
curl -fL https://github.com/Kaixxrua/excel-codex-bridge/releases/download/v0.5.0/excel-codex-bridge-0.5.0-macos-arm64.tar.gz | tar xz
./excel-codex-bridge-0.5.0-macos-arm64/excel-codex status
```

用浏览器下载的，解压后先运行一次 `xattr -dr com.apple.quarantine <解压出的目录>`。

## 注意

- 安装包附带中英文 SUB2API 部署文档。
- **macOS 支持仍是实验性的**：从真实 Mac 版 Excel 读取登录态还没实机验证过，欢迎反馈。
- 程序没有代码签名（Windows 和 macOS 都没有）。可以用同目录的 `.sha256` 文件核对下载是否完整。

交流 QQ 群：966195257

---

## Changes

- **Sign-in.** The default (`auto`) now prefers Codex's own ChatGPT sign-in (`codex login`, read from
  `~/.codex/auth.json`), so **Excel need not be installed**. When the backend refuses it, Codex is not
  signed in, or the sign-in has expired, it falls back to the session the Excel add-in caches, exactly as
  before. `--login` or `EXCEL_BRIDGE_LOGIN` picks `auto` / `codex` (Codex only) / `excel` (Excel only, the
  original behaviour).
- When it falls back (in `auto`): Codex is not signed in / uses an API key / its sign-in has expired or
  expires within 5 minutes / the backend answers 401 or 403. After a refusal it retries once on the Excel
  session and stays on Excel until you `codex login` again (`auth.json` changes), when it tries Codex once more.
- `excel-codex status` now shows which sign-in is in use, when it expires, and how to renew it. The bridge
  never refreshes or writes a token.
- `EXCEL_BRIDGE_CODEX_AUTH` can point at a different `auth.json`. [SUB2API remote sync](https://github.com/Kaixxrua/excel-codex-bridge/blob/v0.5.0/docs/sub2api.en.md)
  only ever sends the Excel add-in's session; Codex's sign-in never leaves this machine.
- **Untested:** whether the Excel backend accepts Codex CLI's own sign-in token has not been tried against
  the real backend. If it does not, `auto` falls back to the Excel session; for the verified path only, use
  `--login excel`. If the backend refuses, please share the reason Codex shows.
- The idea comes from [MIKUbiu/bps-local](https://github.com/MIKUbiu/bps-local) (Unlicense); this project is
  a separate implementation of it. Thanks to MIKUbiu.

## Download

- **Windows**: `excel-codex-bridge-0.5.0-windows-x64.zip`. Double-click `excel-codex.exe` for the
  Codex CLI, or `excel-codex-desktop.cmd` for the desktop app.
- **macOS (Apple silicon)**: `excel-codex-bridge-0.5.0-macos-arm64.tar.gz`
- **macOS (Intel)**: `excel-codex-bridge-0.5.0-macos-x64.tar.gz`
- **Linux / WSL, or from source**: download the source code and use `excel-codex.sh` (Python 3.10+).
- **Linux / VPS with SUB2API**: download this release's source and follow the [deployment guide](https://github.com/Kaixxrua/excel-codex-bridge/blob/v0.5.0/docs/sub2api.en.md).

On a Mac, downloading with `curl` avoids the "developer cannot be verified" block (Intel: replace
`arm64` with `x64`):

```
curl -fL https://github.com/Kaixxrua/excel-codex-bridge/releases/download/v0.5.0/excel-codex-bridge-0.5.0-macos-arm64.tar.gz | tar xz
./excel-codex-bridge-0.5.0-macos-arm64/excel-codex status
```

If you downloaded with a browser, run `xattr -dr com.apple.quarantine <extracted folder>` once.

## Notes

- Packages include bilingual SUB2API deployment guides.
- **macOS support is still experimental**: reading the sign-in from a real Mac Excel has not been
  verified on hardware yet; feedback welcome.
- The programs are not code-signed (neither Windows nor macOS). Check downloads against the
  `.sha256` files next to them.

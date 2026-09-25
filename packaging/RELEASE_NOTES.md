SUB2API 同步也支持 Codex 登录了（免 Excel） · SUB2API sync now works with Codex's sign-in (no Excel)

> **非官方项目**，与 OpenAI、Microsoft 无关联。使用加载项后端可能违反 OpenAI 服务条款，风险自负。
> **Unofficial.** Not affiliated with OpenAI or Microsoft. Using the add-in's backend may violate OpenAI's terms; use at your own risk.

## 变化

- **SUB2API 远端同步现在也能用 Codex 自己的登录**：`excel-sub2api push-session --login`（默认 `auto`：先 Codex
  再 Excel，也读 `EXCEL_BRIDGE_LOGIN`），所以**没有安装 Excel 的机器（比如服务器）也能推会话**。之前同步入口
  写死只读 Excel 登录态。`--login codex` / `--login excel` 可强制其一。
- **凭据外发提醒**：这条命令会把**你选中的那份登录**发往你指定的服务器——用 Codex 登录时，Codex 的 token 也会
  离开本机去到服务器。仅在你显式运行同步命令时发生，且只发给你自己信任的机器。默认本地模式不受影响。
- 已用真实后端验证过（Codex CLI 0.156.1）：不仅纯文本，**看图、文件/图片上传（/attachments）、生图
  （/images/generations）走 Codex 登录都通过**，`bps.openai.com` 均返回 200。
- 纯文档/入口改动，本地默认模式和既有 Excel 用户行为不变。

## 下载

- **Windows**：`excel-codex-bridge-0.5.1-windows-x64.zip`。解压后双击 `excel-codex.exe` 打开 Codex CLI，
  双击 `excel-codex-desktop.cmd` 给桌面版用。
- **macOS（Apple 芯片）**：`excel-codex-bridge-0.5.1-macos-arm64.tar.gz`
- **macOS（Intel）**：`excel-codex-bridge-0.5.1-macos-x64.tar.gz`
- **Linux / WSL 或从源码运行**：下载 Source code，使用 `excel-codex.sh`（需要 Python 3.10+）。
- **Linux / VPS 的 SUB2API 部署**：下载本版本 Source code，按 [部署文档](https://github.com/Kaixxrua/excel-codex-bridge/blob/v0.5.1/docs/sub2api.md) 构建 `packaging/sub2api/compose.yaml`。

macOS 推荐在终端用 `curl` 下载，这样不会被"无法验证开发者"拦下（Intel 芯片把 `arm64` 换成 `x64`）：

```
curl -fL https://github.com/Kaixxrua/excel-codex-bridge/releases/download/v0.5.1/excel-codex-bridge-0.5.1-macos-arm64.tar.gz | tar xz
./excel-codex-bridge-0.5.1-macos-arm64/excel-codex status
```

用浏览器下载的，解压后先运行一次 `xattr -dr com.apple.quarantine <解压出的目录>`。

## 注意

- 安装包附带中英文 SUB2API 部署文档。
- **macOS 支持仍是实验性的**：从真实 Mac 版 Excel 读取登录态还没实机验证过，欢迎反馈。
- 程序没有代码签名（Windows 和 macOS 都没有）。可以用同目录的 `.sha256` 文件核对下载是否完整。

交流 QQ 群：966195257

---

## Changes

- **SUB2API remote sync now works with Codex's own sign-in.** `excel-sub2api push-session --login`
  (default `auto`: Codex's, then the Excel add-in's; `EXCEL_BRIDGE_LOGIN` too) lets **a machine without
  Excel — a server — push a session**. The sync entry point previously read the Excel sign-in only.
  `--login codex` / `--login excel` force one.
- **Credential-export note.** This sends the sign-in you choose to the server you name — with Codex,
  Codex's token leaves this machine for the server. It happens only when you run the sync command, and
  only to a machine you trust. The default local mode is unaffected.
- Verified against the real backend (Codex CLI 0.156.1): beyond plain text, **vision, file/picture
  upload (/attachments) and image generation (/images/generations) all work on Codex's sign-in**, with
  `bps.openai.com` returning 200 for each.
- Docs / entry-point change only; the default local mode and existing Excel users' behaviour are unchanged.

## Download

- **Windows**: `excel-codex-bridge-0.5.1-windows-x64.zip`. Double-click `excel-codex.exe` for the
  Codex CLI, or `excel-codex-desktop.cmd` for the desktop app.
- **macOS (Apple silicon)**: `excel-codex-bridge-0.5.1-macos-arm64.tar.gz`
- **macOS (Intel)**: `excel-codex-bridge-0.5.1-macos-x64.tar.gz`
- **Linux / WSL, or from source**: download the source code and use `excel-codex.sh` (Python 3.10+).
- **Linux / VPS with SUB2API**: download this release's source and follow the [deployment guide](https://github.com/Kaixxrua/excel-codex-bridge/blob/v0.5.1/docs/sub2api.en.md).

On a Mac, downloading with `curl` avoids the "developer cannot be verified" block (Intel: replace
`arm64` with `x64`):

```
curl -fL https://github.com/Kaixxrua/excel-codex-bridge/releases/download/v0.5.1/excel-codex-bridge-0.5.1-macos-arm64.tar.gz | tar xz
./excel-codex-bridge-0.5.1-macos-arm64/excel-codex status
```

If you downloaded with a browser, run `xattr -dr com.apple.quarantine <extracted folder>` once.

## Notes

- Packages include bilingual SUB2API deployment guides.
- **macOS support is still experimental**: reading the sign-in from a real Mac Excel has not been
  verified on hardware yet; feedback welcome.
- The programs are not code-signed (neither Windows nor macOS). Check downloads against the
  `.sha256` files next to them.

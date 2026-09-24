桌面版走 Excel 链路 + 自动登录 · Desktop app support and automatic sign-in

> **非官方项目**，与 OpenAI、Microsoft 无关联。使用加载项后端可能违反 OpenAI 服务条款，风险自负。
> **Unofficial.** Not affiliated with OpenAI or Microsoft. Using the add-in's backend may violate OpenAI's terms; use at your own risk.

## 新功能

- **Codex 桌面版 / IDE 插件也能走 Excel 链路**：双击压缩包里的 `excel-codex-desktop.cmd`（或运行
  `excel-codex desktop`），然后重启 Codex 桌面版。窗口开着期间 `~/.codex/config.toml` 指向本机桥接，
  **关掉窗口就按原样恢复**，原文件另存为 `config.toml.before-excel-codex`。`--keep-config` 可长期保持，
  `excel-codex desktop --off` 恢复。
- **自动登录（Windows）**：没有可用会话时，程序自动打开 Excel 并弹出 ChatGPT 加载项面板，
  在面板里登录后 Excel 自动关闭，不用再自己打开 Excel。登录仍由官方加载项完成，本工具不碰账号密码。
  会话剩余不到 24 小时时，运行中的桥接会在后台最小化打开 Excel 续期。新命令 `excel-codex login`；
  用 `--no-auto-signin` 或 `EXCEL_BRIDGE_AUTO_SIGNIN=0` 关闭。
- **自动弹出面板是新功能，还没在各种 Office 版本上验证**。如果面板没弹出来，点 开始 → 加载项 → ChatGPT，
  其余步骤照常自动完成。欢迎反馈。

## 下载

- **Windows 免安装版**：`excel-codex-bridge-0.2.0-windows-x64.zip`。解压后双击 `excel-codex.exe`
  打开 Codex CLI，双击 `excel-codex-desktop.cmd` 给桌面版用。不需要安装 Python。
- **macOS / Linux / WSL 或从源码运行**：下载 Source code，使用 `excel-codex.sh` 或 `excel-codex.cmd`
  （需要 Python 3.10+）。自动登录只支持 Windows。

使用前提：Microsoft 365 桌面版 Excel 和 ChatGPT 加载项；已安装 Codex CLI（`npm install -g @openai/codex`）。

## 注意

- exe 没有代码签名，SmartScreen 拦截时点"更多信息 → 仍要运行"。可以用同目录的 `.sha256` 文件核对下载是否完整。
- 这个构建已在 GitHub Actions 的 Windows 环境里，用 Codex CLI 0.156.1 和模拟的 Excel 后端跑过端到端测试，
  包括桌面模式（普通 `codex` 读 `config.toml` 走桥接、窗口关闭后配置逐字节还原）。
  自动打开 Excel 面板需要真实 Excel，没有包含在自动测试里。

交流 QQ 群：966195257

---

## What's new

- **The Codex desktop app / IDE extension can use the Excel bridge.** Double-click
  `excel-codex-desktop.cmd` from the zip (or run `excel-codex desktop`), then restart the desktop app.
  While that window is open, `~/.codex/config.toml` points at the local bridge; **closing it restores
  the file exactly**, and the original is kept as `config.toml.before-excel-codex`. `--keep-config`
  keeps it on; `excel-codex desktop --off` undoes it.
- **Automatic sign-in (Windows).** With no usable session, the tool opens Excel with the ChatGPT
  add-in pane; sign in there and Excel closes again. The official add-in still does the sign-in;
  this tool never sees your password. With less than 24 hours left, a running bridge refreshes the
  session through a minimized Excel. New command `excel-codex login`; turn it off with
  `--no-auto-signin` or `EXCEL_BRIDGE_AUTO_SIGNIN=0`.
- **Opening the pane automatically is new and not yet verified on every Office build.** If the
  pane does not appear, click Home → Add-ins → ChatGPT; the rest still happens automatically.

## Download

- **Windows, no install**: `excel-codex-bridge-0.2.0-windows-x64.zip`. Double-click
  `excel-codex.exe` for the Codex CLI, or `excel-codex-desktop.cmd` for the desktop app. Python is not required.
- **macOS / Linux / WSL, or from source**: download the source code and use `excel-codex.sh` or
  `excel-codex.cmd` (Python 3.10+). Automatic sign-in is Windows only.

## Notes

- The exe is not code-signed; if SmartScreen stops it, choose "More info → Run anyway". Check the
  download against the `.sha256` file.
- This build passed end-to-end tests on GitHub Actions Windows runners with Codex CLI 0.156.1 and
  a simulated Excel backend, including desktop mode (plain `codex` reading `config.toml`, and the
  file restored byte for byte afterwards). Opening the pane needs a real Excel and is not covered
  by the automated tests.

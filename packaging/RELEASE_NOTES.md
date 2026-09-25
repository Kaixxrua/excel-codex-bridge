桥接窗口显示请求，桌面版报错说明 · Requests shown in the bridge window, desktop errors explained

> **非官方项目**，与 OpenAI、Microsoft 无关联。使用加载项后端可能违反 OpenAI 服务条款，风险自负。
> **Unofficial.** Not affiliated with OpenAI or Microsoft. Using the add-in's backend may violate OpenAI's terms; use at your own risk.

## 变化

- **桥接窗口显示每个请求**：桌面版模式和 `serve` 的窗口里，每个请求显示一行，例如
  `"POST /v1/responses HTTP/1.1" 200`（只有方法、路径和状态码，不含提示词和 token）。
  以前这些行被日志级别吞掉了。现在一眼就能看出 Codex 的请求有没有经过桥接。
- **桌面版报错说明**：`The '…-excel' model is not supported when using Codex with a ChatGPT account`，
  以及在 Codex 里退出登录后变成的 `401 Unauthorized … api.openai.com/v1/responses`，都说明这条对话
  没经过桥接、直接发给了 OpenAI。桌面版在新建对话时读取 `config.toml` 决定发往哪里，之后一直沿用，
  模型列表却只在启动时读一次。解决：开着桥接窗口，**完全退出桌面版再打开，然后新建对话**。
  README 里写了常见原因。
- 桌面版模式启动时和恢复配置后，都会提醒完全重开桌面版并新建对话。

## 下载

- **Windows**：`excel-codex-bridge-0.4.2-windows-x64.zip`。解压后双击 `excel-codex.exe` 打开 Codex CLI，
  双击 `excel-codex-desktop.cmd` 给桌面版用。
- **macOS（Apple 芯片）**：`excel-codex-bridge-0.4.2-macos-arm64.tar.gz`
- **macOS（Intel）**：`excel-codex-bridge-0.4.2-macos-x64.tar.gz`
- **Linux / WSL 或从源码运行**：下载 Source code，使用 `excel-codex.sh`（需要 Python 3.10+）。
- **Linux / VPS 的 SUB2API 部署**：下载本版本 Source code，按 [部署文档](https://github.com/Kaixxrua/excel-codex-bridge/blob/v0.4.2/docs/sub2api.md) 构建 `packaging/sub2api/compose.yaml`。

macOS 推荐在终端用 `curl` 下载，这样不会被"无法验证开发者"拦下（Intel 芯片把 `arm64` 换成 `x64`）：

```
curl -fL https://github.com/Kaixxrua/excel-codex-bridge/releases/download/v0.4.2/excel-codex-bridge-0.4.2-macos-arm64.tar.gz | tar xz
./excel-codex-bridge-0.4.2-macos-arm64/excel-codex status
```

用浏览器下载的，解压后先运行一次 `xattr -dr com.apple.quarantine <解压出的目录>`。

## 注意

- 安装包附带中英文 SUB2API 部署文档。
- **macOS 支持仍是实验性的**：从真实 Mac 版 Excel 读取登录态还没实机验证过，欢迎反馈。
- 程序没有代码签名（Windows 和 macOS 都没有）。可以用同目录的 `.sha256` 文件核对下载是否完整。

交流 QQ 群：966195257

---

## Changes

- **Each request shows in the bridge window.** In desktop mode and `serve`, every request prints one
  line such as `"POST /v1/responses HTTP/1.1" 200` (method, path and status only; no prompts or
  tokens). The log level used to hide these lines. Now it is easy to see whether Codex's requests
  reach the bridge at all.
- **Desktop errors explained.** `The '…-excel' model is not supported when using Codex with a
  ChatGPT account`, and the `401 Unauthorized … api.openai.com/v1/responses` it turns into after
  signing out of Codex, both mean the conversation bypassed the bridge and went to OpenAI. The
  desktop app decides where a conversation's requests go from `config.toml` when the conversation
  starts and keeps that choice, but reads the model list only at startup. Fix: with the bridge
  window open, **fully quit and reopen the desktop app, then start a new conversation**. The README
  lists the usual causes.
- Desktop mode now says to fully reopen the desktop app and start a new conversation, both when it
  starts and after it restores the config.

## Download

- **Windows**: `excel-codex-bridge-0.4.2-windows-x64.zip`. Double-click `excel-codex.exe` for the
  Codex CLI, or `excel-codex-desktop.cmd` for the desktop app.
- **macOS (Apple silicon)**: `excel-codex-bridge-0.4.2-macos-arm64.tar.gz`
- **macOS (Intel)**: `excel-codex-bridge-0.4.2-macos-x64.tar.gz`
- **Linux / WSL, or from source**: download the source code and use `excel-codex.sh` (Python 3.10+).
- **Linux / VPS with SUB2API**: download this release's source and follow the [deployment guide](https://github.com/Kaixxrua/excel-codex-bridge/blob/v0.4.2/docs/sub2api.en.md).

On a Mac, downloading with `curl` avoids the "developer cannot be verified" block (Intel: replace
`arm64` with `x64`):

```
curl -fL https://github.com/Kaixxrua/excel-codex-bridge/releases/download/v0.4.2/excel-codex-bridge-0.4.2-macos-arm64.tar.gz | tar xz
./excel-codex-bridge-0.4.2-macos-arm64/excel-codex status
```

If you downloaded with a browser, run `xattr -dr com.apple.quarantine <extracted folder>` once.

## Notes

- Packages include bilingual SUB2API deployment guides.
- **macOS support is still experimental**: reading the sign-in from a real Mac Excel has not been
  verified on hardware yet; feedback welcome.
- The programs are not code-signed (neither Windows nor macOS). Check downloads against the
  `.sha256` files next to them.

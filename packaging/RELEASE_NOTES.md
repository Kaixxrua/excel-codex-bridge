并行工具调用，多会话同时开工 · Parallel tool calls, many sessions at once

> **非官方项目**，与 OpenAI、Microsoft 无关联。使用加载项后端可能违反 OpenAI 服务条款，风险自负。
> **Unofficial.** Not affiliated with OpenAI or Microsoft. Using the add-in's backend may violate OpenAI's terms; use at your own risk.

## 变化

- **并行工具调用**：互不依赖的工具调用（比如同时读几个文件、跑几条命令）模型会一次发出，Codex 同时执行，
  不用再一轮一轮地等。改文件仍按顺序来。0.4.3 只执行第一个调用、丢掉其余的，现在全部执行。
  已用真实后端确认：模型会一次发出多个调用，后端也接受带多个调用结果的历史。
- **多个会话同时用**：几个 Codex 窗口、桌面版对话或子代理可以共用一个桥接同时工作。以前最多 8 个请求
  同时进行，第 9 个要排队，排超过 30 秒就报错；现在上限是 64。
- 同时开的会话越多，ChatGPT 套餐额度用得越快。

## 下载

- **Windows**：`excel-codex-bridge-0.4.5-windows-x64.zip`。解压后双击 `excel-codex.exe` 打开 Codex CLI，
  双击 `excel-codex-desktop.cmd` 给桌面版用。
- **macOS（Apple 芯片）**：`excel-codex-bridge-0.4.5-macos-arm64.tar.gz`
- **macOS（Intel）**：`excel-codex-bridge-0.4.5-macos-x64.tar.gz`
- **Linux / WSL 或从源码运行**：下载 Source code，使用 `excel-codex.sh`（需要 Python 3.10+）。
- **Linux / VPS 的 SUB2API 部署**：下载本版本 Source code，按 [部署文档](https://github.com/Kaixxrua/excel-codex-bridge/blob/v0.4.5/docs/sub2api.md) 构建 `packaging/sub2api/compose.yaml`。

macOS 推荐在终端用 `curl` 下载，这样不会被"无法验证开发者"拦下（Intel 芯片把 `arm64` 换成 `x64`）：

```
curl -fL https://github.com/Kaixxrua/excel-codex-bridge/releases/download/v0.4.5/excel-codex-bridge-0.4.5-macos-arm64.tar.gz | tar xz
./excel-codex-bridge-0.4.5-macos-arm64/excel-codex status
```

用浏览器下载的，解压后先运行一次 `xattr -dr com.apple.quarantine <解压出的目录>`。

## 注意

- 安装包附带中英文 SUB2API 部署文档。
- **macOS 支持仍是实验性的**：从真实 Mac 版 Excel 读取登录态还没实机验证过，欢迎反馈。
- 程序没有代码签名（Windows 和 macOS 都没有）。可以用同目录的 `.sha256` 文件核对下载是否完整。

交流 QQ 群：966195257

---

## Changes

- **Parallel tool calls.** Calls that do not depend on each other (reading several files, independent
  commands) now come in one go and Codex runs them at the same time instead of one round after another.
  File edits still happen in order. 0.4.3 ran only the first such call and dropped the rest; now all of
  them run. Checked against the real backend: the model does issue several calls at once, and the backend
  takes a history with several results.
- **Several sessions at once.** Codex windows, desktop conversations and subagents can share one bridge
  and work at the same time. Only 8 requests could run at once before; the 9th queued and failed after
  30 seconds. The limit is now 64.
- More sessions at once use up your ChatGPT plan faster.

## Download

- **Windows**: `excel-codex-bridge-0.4.5-windows-x64.zip`. Double-click `excel-codex.exe` for the
  Codex CLI, or `excel-codex-desktop.cmd` for the desktop app.
- **macOS (Apple silicon)**: `excel-codex-bridge-0.4.5-macos-arm64.tar.gz`
- **macOS (Intel)**: `excel-codex-bridge-0.4.5-macos-x64.tar.gz`
- **Linux / WSL, or from source**: download the source code and use `excel-codex.sh` (Python 3.10+).
- **Linux / VPS with SUB2API**: download this release's source and follow the [deployment guide](https://github.com/Kaixxrua/excel-codex-bridge/blob/v0.4.5/docs/sub2api.en.md).

On a Mac, downloading with `curl` avoids the "developer cannot be verified" block (Intel: replace
`arm64` with `x64`):

```
curl -fL https://github.com/Kaixxrua/excel-codex-bridge/releases/download/v0.4.5/excel-codex-bridge-0.4.5-macos-arm64.tar.gz | tar xz
./excel-codex-bridge-0.4.5-macos-arm64/excel-codex status
```

If you downloaded with a browser, run `xattr -dr com.apple.quarantine <extracted folder>` once.

## Notes

- Packages include bilingual SUB2API deployment guides.
- **macOS support is still experimental**: reading the sign-in from a real Mac Excel has not been
  verified on hardware yet; feedback welcome.
- The programs are not code-signed (neither Windows nor macOS). Check downloads against the
  `.sha256` files next to them.

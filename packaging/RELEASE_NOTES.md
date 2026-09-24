首个版本 · First release

> **非官方项目**，与 OpenAI、Microsoft 无关联。使用加载项后端可能违反 OpenAI 服务条款，风险自负。
> **Unofficial.** Not affiliated with OpenAI or Microsoft. Using the add-in's backend may violate OpenAI's terms; use at your own risk.

## 下载

- **Windows 免安装版**：`excel-codex-bridge-0.1.0-windows-x64.zip`。解压后双击 `excel-codex.exe`，
  或在你的项目目录里打开终端运行它。不需要安装 Python。
- **macOS / Linux / WSL 或从源码运行**：下载 Source code，使用 `excel-codex.sh` 或 `excel-codex.cmd`
  （需要 Python 3.10+）。

使用前提：Microsoft 365 桌面版 Excel 里的 ChatGPT 加载项已登录；已安装 Codex CLI（`npm install -g @openai/codex`）。

## 模型

- `gpt-5.6-sol-excel`（默认）、`gpt-5.6-terra-excel`、`gpt-5.6-luna-excel`
- `gpt-6-astra-excel`（**实验性**）：还没确认 Excel 后端支持它。用 `excel-codex --model gpt-6-astra-excel` 试用；
  如果报模型不可用，说明你的账号或 Excel 后端还没开放。

## 注意

- exe 没有代码签名。首次运行时 Windows SmartScreen 可能提示"Windows 已保护你的电脑"，
  点"更多信息 → 仍要运行"即可。可以用同目录的 `.sha256` 文件核对下载是否完整。
- 这个构建已在 GitHub Actions 的 Windows 环境里，用 Codex CLI 0.156.1 和模拟的 Excel 后端跑过端到端测试。

---

## Download

- **Windows, no install**: `excel-codex-bridge-0.1.0-windows-x64.zip`. Unzip, then double-click
  `excel-codex.exe` or run it from a terminal in your project folder. Python is not required.
- **macOS / Linux / WSL, or from source**: download the source code and use `excel-codex.sh` or
  `excel-codex.cmd` (Python 3.10+).

Requirements: the ChatGPT add-in signed in inside Microsoft 365 desktop Excel, and the Codex CLI
(`npm install -g @openai/codex`).

## Models

- `gpt-5.6-sol-excel` (default), `gpt-5.6-terra-excel`, `gpt-5.6-luna-excel`
- `gpt-6-astra-excel` (**experimental**): not yet confirmed on the Excel backend. Try it with
  `excel-codex --model gpt-6-astra-excel`; a model error means it is not available to your account there yet.

## Notes

- The exe is not code-signed. On first run Windows SmartScreen may say "Windows protected your PC";
  choose "More info → Run anyway". Check the download against the `.sha256` file.
- This build passed end-to-end tests on GitHub Actions Windows runners with Codex CLI 0.156.1 and a
  simulated Excel backend.

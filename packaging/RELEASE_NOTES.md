macOS 免安装版 · No-install macOS builds

> **非官方项目**，与 OpenAI、Microsoft 无关联。使用加载项后端可能违反 OpenAI 服务条款，风险自负。
> **Unofficial.** Not affiliated with OpenAI or Microsoft. Using the add-in's backend may violate OpenAI's terms; use at your own risk.

## 新功能

- **macOS 免安装版**：Apple 芯片（`macos-arm64`）和 Intel（`macos-x64`）各一个包，不需要安装 Python，
  也不需要 Windows 电脑。在项目目录运行 `excel-codex` 打开 Codex CLI；双击 `excel-codex-desktop.command`
  给 Codex 桌面版用，关掉终端窗口配置就按原样恢复。

## 下载

- **Windows**：`excel-codex-bridge-0.2.2-windows-x64.zip`。解压后双击 `excel-codex.exe` 打开 Codex CLI，
  双击 `excel-codex-desktop.cmd` 给桌面版用。
- **macOS（Apple 芯片）**：`excel-codex-bridge-0.2.2-macos-arm64.tar.gz`
- **macOS（Intel）**：`excel-codex-bridge-0.2.2-macos-x64.tar.gz`
- **Linux / WSL 或从源码运行**：下载 Source code，使用 `excel-codex.sh`（需要 Python 3.10+）。

macOS 推荐在终端用 `curl` 下载，这样不会被"无法验证开发者"拦下（Intel 芯片把 `arm64` 换成 `x64`）：

```
curl -fL https://github.com/Kaixxrua/excel-codex-bridge/releases/download/v0.2.2/excel-codex-bridge-0.2.2-macos-arm64.tar.gz | tar xz
./excel-codex-bridge-0.2.2-macos-arm64/excel-codex status
```

用浏览器下载的，解压后先运行一次 `xattr -dr com.apple.quarantine <解压出的目录>`。

## 注意

- Mac 上没有自动登录：在 Mac 版 Excel 里点 开始 → 加载项 → ChatGPT 登录一次，会话大约 10 天有效。
  第一次读取会话时 macOS 可能询问是否允许"终端"访问其他 App 的数据，选允许。
- **macOS 支持仍是实验性的**：两个 Mac 包都在 GitHub Actions 的 macOS 环境里，用 Codex CLI 0.156.1
  和模拟的 Excel 后端跑过端到端测试（包括桌面模式）。但从真实 Mac 版 Excel 读取登录态还没实机验证过，欢迎反馈。
- 程序没有代码签名（Windows 和 macOS 都没有）。可以用同目录的 `.sha256` 文件核对下载是否完整。

交流 QQ 群：966195257

---

## What's new

- **No-install macOS builds** for Apple silicon (`macos-arm64`) and Intel (`macos-x64`). No Python
  and no Windows PC needed. Run `excel-codex` from your project folder for the Codex CLI, or
  double-click `excel-codex-desktop.command` for the Codex desktop app; closing the Terminal window
  restores your config exactly.

## Download

- **Windows**: `excel-codex-bridge-0.2.2-windows-x64.zip`. Double-click `excel-codex.exe` for the
  Codex CLI, or `excel-codex-desktop.cmd` for the desktop app.
- **macOS (Apple silicon)**: `excel-codex-bridge-0.2.2-macos-arm64.tar.gz`
- **macOS (Intel)**: `excel-codex-bridge-0.2.2-macos-x64.tar.gz`
- **Linux / WSL, or from source**: download the source code and use `excel-codex.sh` (Python 3.10+).

On a Mac, downloading with `curl` avoids the "developer cannot be verified" block (Intel: replace
`arm64` with `x64`):

```
curl -fL https://github.com/Kaixxrua/excel-codex-bridge/releases/download/v0.2.2/excel-codex-bridge-0.2.2-macos-arm64.tar.gz | tar xz
./excel-codex-bridge-0.2.2-macos-arm64/excel-codex status
```

If you downloaded it with a browser, run `xattr -dr com.apple.quarantine <extracted folder>` once.

## Notes

- There is no automatic sign-in on the Mac: in Mac Excel, click Home → Add-ins → ChatGPT and sign
  in once; the session lasts about 10 days. The first time the session is read, macOS may ask
  whether Terminal may access data from other apps; allow it.
- **macOS support is still experimental.** Both Mac builds passed end-to-end tests on GitHub
  Actions macOS runners with Codex CLI 0.156.1 and a simulated Excel backend, including desktop
  mode. Reading the session from a real Mac Excel has not been verified on a real machine yet;
  feedback welcome.
- Neither the Windows nor the macOS build is code-signed. Check downloads against the `.sha256` files.

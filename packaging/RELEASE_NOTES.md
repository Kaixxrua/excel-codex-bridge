有新版本时自动提示 · Tells you when a new version is out

> **非官方项目**，与 OpenAI、Microsoft 无关联。使用加载项后端可能违反 OpenAI 服务条款，风险自负。
> **Unofficial.** Not affiliated with OpenAI or Microsoft. Using the add-in's backend may violate OpenAI's terms; use at your own risk.

## 变化

- **自动检查更新**：程序在后台查 GitHub 上的最新版本（最多每 12 小时一次），有新版就显示版本号、
  更新内容和下载链接。
  - 桌面版模式和 `serve` 的窗口：启动后显示；窗口一直开着时，新版本发布后也会显示。
  - 用 `excel-codex` 启动 Codex 时：Codex 退出后显示。双击打开的窗口会停住，按回车再关。
  - 请求只发给 `api.github.com`，除了版本号不带任何信息，代理设置和访问上游时相同；查不到就不提示。
    设置 `EXCEL_BRIDGE_UPDATE_CHECK=0` 可以关掉。
- 这一版还需要手动下载；装上以后，再有新版本就会自动提示了。

## 下载

- **Windows**：`excel-codex-bridge-0.4.4-windows-x64.zip`。解压后双击 `excel-codex.exe` 打开 Codex CLI，
  双击 `excel-codex-desktop.cmd` 给桌面版用。
- **macOS（Apple 芯片）**：`excel-codex-bridge-0.4.4-macos-arm64.tar.gz`
- **macOS（Intel）**：`excel-codex-bridge-0.4.4-macos-x64.tar.gz`
- **Linux / WSL 或从源码运行**：下载 Source code，使用 `excel-codex.sh`（需要 Python 3.10+）。
- **Linux / VPS 的 SUB2API 部署**：下载本版本 Source code，按 [部署文档](https://github.com/Kaixxrua/excel-codex-bridge/blob/v0.4.4/docs/sub2api.md) 构建 `packaging/sub2api/compose.yaml`。

macOS 推荐在终端用 `curl` 下载，这样不会被"无法验证开发者"拦下（Intel 芯片把 `arm64` 换成 `x64`）：

```
curl -fL https://github.com/Kaixxrua/excel-codex-bridge/releases/download/v0.4.4/excel-codex-bridge-0.4.4-macos-arm64.tar.gz | tar xz
./excel-codex-bridge-0.4.4-macos-arm64/excel-codex status
```

用浏览器下载的，解压后先运行一次 `xattr -dr com.apple.quarantine <解压出的目录>`。

## 注意

- 安装包附带中英文 SUB2API 部署文档。
- **macOS 支持仍是实验性的**：从真实 Mac 版 Excel 读取登录态还没实机验证过，欢迎反馈。
- 程序没有代码签名（Windows 和 macOS 都没有）。可以用同目录的 `.sha256` 文件核对下载是否完整。

交流 QQ 群：966195257

---

## Changes

- **Update check.** The tool checks GitHub in the background for the latest release (at most every
  12 hours) and, when there is a newer one, shows its version, what changed and the download link.
  - Desktop-mode and `serve` windows: after startup, and whenever a release comes out while the window
    stays open.
  - When `excel-codex` starts Codex: after Codex exits. A window opened by double-click then waits for
    Enter before it closes.
  - The request goes only to `api.github.com`, carries nothing but the version, and uses the same proxy
    settings as the bridge; if GitHub cannot be reached, nothing is shown. Set
    `EXCEL_BRIDGE_UPDATE_CHECK=0` to turn it off.
- This version still has to be downloaded by hand; from here on, new versions announce themselves.

## Download

- **Windows**: `excel-codex-bridge-0.4.4-windows-x64.zip`. Double-click `excel-codex.exe` for the
  Codex CLI, or `excel-codex-desktop.cmd` for the desktop app.
- **macOS (Apple silicon)**: `excel-codex-bridge-0.4.4-macos-arm64.tar.gz`
- **macOS (Intel)**: `excel-codex-bridge-0.4.4-macos-x64.tar.gz`
- **Linux / WSL, or from source**: download the source code and use `excel-codex.sh` (Python 3.10+).
- **Linux / VPS with SUB2API**: download this release's source and follow the [deployment guide](https://github.com/Kaixxrua/excel-codex-bridge/blob/v0.4.4/docs/sub2api.en.md).

On a Mac, downloading with `curl` avoids the "developer cannot be verified" block (Intel: replace
`arm64` with `x64`):

```
curl -fL https://github.com/Kaixxrua/excel-codex-bridge/releases/download/v0.4.4/excel-codex-bridge-0.4.4-macos-arm64.tar.gz | tar xz
./excel-codex-bridge-0.4.4-macos-arm64/excel-codex status
```

If you downloaded with a browser, run `xattr -dr com.apple.quarantine <extracted folder>` once.

## Notes

- Packages include bilingual SUB2API deployment guides.
- **macOS support is still experimental**: reading the sign-in from a real Mac Excel has not been
  verified on hardware yet; feedback welcome.
- The programs are not code-signed (neither Windows nor macOS). Check downloads against the
  `.sha256` files next to them.

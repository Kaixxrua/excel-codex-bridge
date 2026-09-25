桌面版图片提示说明 · Desktop picture prompt explained

> **非官方项目**，与 OpenAI、Microsoft 无关联。使用加载项后端可能违反 OpenAI 服务条款，风险自负。
> **Unofficial.** Not affiliated with OpenAI or Microsoft. Using the add-in's backend may violate OpenAI's terms; use at your own risk.

## 变化

- **桌面版「此模型不支持图像输入」说明**：这个提示是 Codex 桌面版按模型目录拦下的，请求根本没有发出。
  0.3.1 及更早版本的目录把 Excel 模型标成只收文字；用 0.3.2 起的版本重新运行桌面版模式后，
  要**完全退出桌面版再打开**才会重新读目录。README「图片」一节写了排查步骤。
- **用 Cockpit Tools 接入时**，模型目录由 Cockpit 生成：请在它的模型供应商设置里把 `*-excel` 模型的
  「图片输入」打开（Cockpit 1.3.57 及更早默认关闭）。
- 内部的模型能力表同样标明 Excel 模型支持图片，写给 Codex 的目录改为从这张表生成，两处不会再不一致。

## 下载

- **Windows**：`excel-codex-bridge-0.4.1-windows-x64.zip`。解压后双击 `excel-codex.exe` 打开 Codex CLI，
  双击 `excel-codex-desktop.cmd` 给桌面版用。
- **macOS（Apple 芯片）**：`excel-codex-bridge-0.4.1-macos-arm64.tar.gz`
- **macOS（Intel）**：`excel-codex-bridge-0.4.1-macos-x64.tar.gz`
- **Linux / WSL 或从源码运行**：下载 Source code，使用 `excel-codex.sh`（需要 Python 3.10+）。
- **Linux / VPS 的 SUB2API 部署**：下载本版本 Source code，按 [部署文档](https://github.com/Kaixxrua/excel-codex-bridge/blob/v0.4.1/docs/sub2api.md) 构建 `packaging/sub2api/compose.yaml`。

macOS 推荐在终端用 `curl` 下载，这样不会被"无法验证开发者"拦下（Intel 芯片把 `arm64` 换成 `x64`）：

```
curl -fL https://github.com/Kaixxrua/excel-codex-bridge/releases/download/v0.4.1/excel-codex-bridge-0.4.1-macos-arm64.tar.gz | tar xz
./excel-codex-bridge-0.4.1-macos-arm64/excel-codex status
```

用浏览器下载的，解压后先运行一次 `xattr -dr com.apple.quarantine <解压出的目录>`。

## 注意

- 安装包附带中英文 SUB2API 部署文档。
- **macOS 支持仍是实验性的**：从真实 Mac 版 Excel 读取登录态还没实机验证过，欢迎反馈。
- 程序没有代码签名（Windows 和 macOS 都没有）。可以用同目录的 `.sha256` 文件核对下载是否完整。

交流 QQ 群：966195257

---

## Changes

- **"This model does not support image inputs" in the desktop app, explained.** The Codex desktop app
  shows it from the model catalog and never sends the request. Versions 0.3.1 and earlier wrote a
  catalog that marked the Excel models as text-only. After starting desktop mode with 0.3.2 or
  later, **fully quit the desktop app and open it again** so that it reads the catalog again. The
  Pictures section of the README has the steps.
- **With Cockpit Tools**, Cockpit writes the model catalog: turn on image input for the `*-excel`
  models in its model provider settings (Cockpit 1.3.57 and earlier leave it off by default).
- The internal model capability table now also says the Excel models take pictures, and the catalog
  written for Codex comes from that table, so the two can no longer disagree.

## Download

- **Windows**: `excel-codex-bridge-0.4.1-windows-x64.zip`. Double-click `excel-codex.exe` for the
  Codex CLI, or `excel-codex-desktop.cmd` for the desktop app.
- **macOS (Apple silicon)**: `excel-codex-bridge-0.4.1-macos-arm64.tar.gz`
- **macOS (Intel)**: `excel-codex-bridge-0.4.1-macos-x64.tar.gz`
- **Linux / WSL, or from source**: download the source code and use `excel-codex.sh` (Python 3.10+).
- **Linux / VPS with SUB2API**: download this release's source and follow the [deployment guide](https://github.com/Kaixxrua/excel-codex-bridge/blob/v0.4.1/docs/sub2api.en.md).

On a Mac, downloading with `curl` avoids the "developer cannot be verified" block (Intel: replace
`arm64` with `x64`):

```
curl -fL https://github.com/Kaixxrua/excel-codex-bridge/releases/download/v0.4.1/excel-codex-bridge-0.4.1-macos-arm64.tar.gz | tar xz
./excel-codex-bridge-0.4.1-macos-arm64/excel-codex status
```

If you downloaded with a browser, run `xattr -dr com.apple.quarantine <extracted folder>` once.

## Notes

- Packages include bilingual SUB2API deployment guides.
- **macOS support is still experimental**: reading the sign-in from a real Mac Excel has not been
  verified on hardware yet; feedback welcome.
- The programs are not code-signed (neither Windows nor macOS). Check downloads against the
  `.sha256` files next to them.

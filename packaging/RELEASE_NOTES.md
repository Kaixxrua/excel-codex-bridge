一次要调多个工具时不再出错 · No more failed tool calls when the model asks for several at once

> **非官方项目**，与 OpenAI、Microsoft 无关联。使用加载项后端可能违反 OpenAI 服务条款，风险自负。
> **Unofficial.** Not affiliated with OpenAI or Microsoft. Using the add-in's backend may violate OpenAI's terms; use at your own risk.

## 变化

- **模型一次要调多个工具时不再出错**：桥接告诉 Codex 不支持并行工具调用，但模型偶尔还是会在一次回复里
  同时调好几个工具（比如一次读两个文件、跑两条命令）。以前桥接遇到这种回复一个都不转换，Codex 收到
  它不认识的 `run_officejs`，回 `unsupported call`，模型又被告知"格式不对"，于是反复重试，看起来就是
  工具调用失败或卡住。现在先执行第一个，其余的丢掉；模型拿到结果后会接着调还需要的。桥接窗口里会
  显示一行说明。
- README 的"限制"一节写清楚了：执行命令（PowerShell / shell）、改文件、看图片都是工具调用，都能用，
  只是一个接一个地调。

## 下载

- **Windows**：`excel-codex-bridge-0.4.3-windows-x64.zip`。解压后双击 `excel-codex.exe` 打开 Codex CLI，
  双击 `excel-codex-desktop.cmd` 给桌面版用。
- **macOS（Apple 芯片）**：`excel-codex-bridge-0.4.3-macos-arm64.tar.gz`
- **macOS（Intel）**：`excel-codex-bridge-0.4.3-macos-x64.tar.gz`
- **Linux / WSL 或从源码运行**：下载 Source code，使用 `excel-codex.sh`（需要 Python 3.10+）。
- **Linux / VPS 的 SUB2API 部署**：下载本版本 Source code，按 [部署文档](https://github.com/Kaixxrua/excel-codex-bridge/blob/v0.4.3/docs/sub2api.md) 构建 `packaging/sub2api/compose.yaml`。

macOS 推荐在终端用 `curl` 下载，这样不会被"无法验证开发者"拦下（Intel 芯片把 `arm64` 换成 `x64`）：

```
curl -fL https://github.com/Kaixxrua/excel-codex-bridge/releases/download/v0.4.3/excel-codex-bridge-0.4.3-macos-arm64.tar.gz | tar xz
./excel-codex-bridge-0.4.3-macos-arm64/excel-codex status
```

用浏览器下载的，解压后先运行一次 `xattr -dr com.apple.quarantine <解压出的目录>`。

## 注意

- 安装包附带中英文 SUB2API 部署文档。
- **macOS 支持仍是实验性的**：从真实 Mac 版 Excel 读取登录态还没实机验证过，欢迎反馈。
- 程序没有代码签名（Windows 和 macOS 都没有）。可以用同目录的 `.sha256` 文件核对下载是否完整。

交流 QQ 群：966195257

---

## Changes

- **Asking for several tools at once no longer fails.** The bridge tells Codex that parallel tool
  calls are not supported, but the model still sometimes calls several tools in one reply (say, reading
  two files or running two commands). The bridge used to convert none of them: Codex got a
  `run_officejs` call it does not know, answered `unsupported call`, and the model, told its call was
  malformed, kept retrying, which looked like tool calls failing or hanging. Now the first call runs
  and the rest are dropped; once the model sees the result it asks again for whatever it still needs.
  The bridge window prints a line when this happens.
- The README's limitations section now spells out that running commands (PowerShell / shell), editing
  files and viewing images are all tool calls and all work, one after another.

## Download

- **Windows**: `excel-codex-bridge-0.4.3-windows-x64.zip`. Double-click `excel-codex.exe` for the
  Codex CLI, or `excel-codex-desktop.cmd` for the desktop app.
- **macOS (Apple silicon)**: `excel-codex-bridge-0.4.3-macos-arm64.tar.gz`
- **macOS (Intel)**: `excel-codex-bridge-0.4.3-macos-x64.tar.gz`
- **Linux / WSL, or from source**: download the source code and use `excel-codex.sh` (Python 3.10+).
- **Linux / VPS with SUB2API**: download this release's source and follow the [deployment guide](https://github.com/Kaixxrua/excel-codex-bridge/blob/v0.4.3/docs/sub2api.en.md).

On a Mac, downloading with `curl` avoids the "developer cannot be verified" block (Intel: replace
`arm64` with `x64`):

```
curl -fL https://github.com/Kaixxrua/excel-codex-bridge/releases/download/v0.4.3/excel-codex-bridge-0.4.3-macos-arm64.tar.gz | tar xz
./excel-codex-bridge-0.4.3-macos-arm64/excel-codex status
```

If you downloaded with a browser, run `xattr -dr com.apple.quarantine <extracted folder>` once.

## Notes

- Packages include bilingual SUB2API deployment guides.
- **macOS support is still experimental**: reading the sign-in from a real Mac Excel has not been
  verified on hardware yet; feedback welcome.
- The programs are not code-signed (neither Windows nor macOS). Check downloads against the
  `.sha256` files next to them.

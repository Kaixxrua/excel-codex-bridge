图片改走 OpenAI 自己的上传、修复自动登录 · Pictures through OpenAI's own upload, sign-in fixed

> **非官方项目**，与 OpenAI、Microsoft 无关联。使用加载项后端可能违反 OpenAI 服务条款，风险自负。
> **Unofficial.** Not affiliated with OpenAI or Microsoft. Using the add-in's backend may violate OpenAI's terms; use at your own risk.

## 变化

- **图片不再需要隧道或中转**：能内嵌就内嵌；Excel 后端不收内嵌图片的地方（用户消息里的图），
  桥接像 Excel 加载项里的「上传文件」按钮那样，用同一个登录会话把图片传到 OpenAI，再按文件 ID 引用。
  同一张图只传一次。图片和请求的其他内容一样只发往 OpenAI，不经过 Cloudflare 或本项目的服务器，也不用任何设置。
- **移除**：本机 Cloudflare 隧道、公共图片中转、Docker 自建图床和 `excel-codex image-host` 命令；
  安装包不再附带 cloudflared，体积更小。`EXCEL_BRIDGE_IMAGE_HOST`、`EXCEL_BRIDGE_IMAGE_TOKEN`、
  `EXCEL_BRIDGE_RELAY_URL`、`EXCEL_BRIDGE_CLOUDFLARED` 不再使用。

## 修复

- **Windows 自动登录**：OpenAI 在 9 月 15 日把 Excel 里的 ChatGPT 加载项升到 2.0.0.1 之后，
  自动打开的登录工作簿只显示 "Error loading add-ins"，面板出不来。现在每次都按 AppSource
  目录里的当前版本生成这个工作簿（目录查不到时用 2.0.0.1）。

## 下载

- **Windows**：`excel-codex-bridge-0.3.2-windows-x64.zip`。解压后双击 `excel-codex.exe` 打开 Codex CLI，
  双击 `excel-codex-desktop.cmd` 给桌面版用。
- **macOS（Apple 芯片）**：`excel-codex-bridge-0.3.2-macos-arm64.tar.gz`
- **macOS（Intel）**：`excel-codex-bridge-0.3.2-macos-x64.tar.gz`
- **Linux / WSL 或从源码运行**：下载 Source code，使用 `excel-codex.sh`（需要 Python 3.10+）。

macOS 推荐在终端用 `curl` 下载，这样不会被"无法验证开发者"拦下（Intel 芯片把 `arm64` 换成 `x64`）：

```
curl -fL https://github.com/Kaixxrua/excel-codex-bridge/releases/download/v0.3.2/excel-codex-bridge-0.3.2-macos-arm64.tar.gz | tar xz
./excel-codex-bridge-0.3.2-macos-arm64/excel-codex status
```

用浏览器下载的，解压后先运行一次 `xattr -dr com.apple.quarantine <解压出的目录>`。

## 注意

- 图片上传是新写的。如果模型说看不到图，请把 `bridge.log` 里含 `picture` 或 `uploading` 的几行发到 issue 或交流群。
- **macOS 支持仍是实验性的**：从真实 Mac 版 Excel 读取登录态还没实机验证过，欢迎反馈。
- 程序没有代码签名（Windows 和 macOS 都没有）。可以用同目录的 `.sha256` 文件核对下载是否完整。

交流 QQ 群：966195257

---

## Changes

- **Pictures no longer need a tunnel or a relay.** They go inline where the backend takes them.
  Where it does not (pictures in user messages), the bridge uploads them to OpenAI the way the
  Upload file button in the Excel add-in does, on the same session, and names them by file id.
  Each picture is uploaded once. Pictures go to OpenAI only, like the rest of the request: not
  through Cloudflare or this project's servers, and with nothing to set up.
- **Removed:** the local Cloudflare tunnel, the public picture relay, the Docker image host and the
  `excel-codex image-host` command. The packages no longer include cloudflared, so they are
  smaller. `EXCEL_BRIDGE_IMAGE_HOST`, `EXCEL_BRIDGE_IMAGE_TOKEN`, `EXCEL_BRIDGE_RELAY_URL` and
  `EXCEL_BRIDGE_CLOUDFLARED` are no longer read.

## Fixes

- **Automatic sign-in on Windows.** Since OpenAI moved the ChatGPT add-in for Excel to 2.0.0.1 on
  15 September, the sign-in workbook only showed "Error loading add-ins" and the pane never came
  up. The workbook now names the add-in's current version from the AppSource catalog (2.0.0.1
  when the catalog cannot be reached).

## Download

- **Windows**: `excel-codex-bridge-0.3.2-windows-x64.zip`. Double-click `excel-codex.exe` for the
  Codex CLI, or `excel-codex-desktop.cmd` for the desktop app.
- **macOS (Apple silicon)**: `excel-codex-bridge-0.3.2-macos-arm64.tar.gz`
- **macOS (Intel)**: `excel-codex-bridge-0.3.2-macos-x64.tar.gz`
- **Linux / WSL, or from source**: download the source code and use `excel-codex.sh` (Python 3.10+).

On a Mac, downloading with `curl` avoids the "developer cannot be verified" block (Intel: replace
`arm64` with `x64`):

```
curl -fL https://github.com/Kaixxrua/excel-codex-bridge/releases/download/v0.3.2/excel-codex-bridge-0.3.2-macos-arm64.tar.gz | tar xz
./excel-codex-bridge-0.3.2-macos-arm64/excel-codex status
```

If you downloaded with a browser, run `xattr -dr com.apple.quarantine <extracted folder>` once.

## Notes

- The picture upload is new. If the model says it cannot see a picture, please send the lines of
  `bridge.log` that mention `picture` or `uploading` (an issue is fine).
- **macOS support is still experimental**: reading the sign-in from a real Mac Excel has not been
  verified on hardware yet; feedback welcome.
- The programs are not code-signed (neither Windows nor macOS). Check downloads against the
  `.sha256` files next to them.

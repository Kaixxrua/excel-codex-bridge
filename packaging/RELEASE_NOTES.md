Codex 自带的生图工具能用了 · Codex's image tool works

> **非官方项目**，与 OpenAI、Microsoft 无关联。使用加载项后端可能违反 OpenAI 服务条款，风险自负。
> **Unofficial.** Not affiliated with OpenAI or Microsoft. Using the add-in's backend may violate OpenAI's terms; use at your own risk.

## 变化

- **生图**：让 Codex「画一张……」或「把这张图改成……」即可，CLI 和桌面版都能用。以前 Codex 不提供这个
  工具；手动加了请求头的，生图请求也会因为桥接没有这个接口而失败（404）。现在桥接把请求转给 Excel
  加载项自己生图用的接口（gpt-image-2），用同一个登录会话，参数也和加载项一致。
- 启动器、桌面版模式和 `print-config` 会自动加上 Codex 要求的 `x-openai-actor-authorization` 请求头，
  不用手动配置。以前自己加过的，可以删掉。
- 不支持透明背景（加载项本身也不支持）；生图额度按 ChatGPT 套餐算。
- 已用真实的 Codex CLI 0.156.1 测过完整流程（Codex 调用生图 → 桥接 → 图片回到对话并存盘）；
  后端这一段按加载项的请求格式实现，还没在真实账号上测过。遇到后端拒绝，请把 Codex 里显示的原因发到群里。

## 下载

- **Windows**：`excel-codex-bridge-0.4.6-windows-x64.zip`。解压后双击 `excel-codex.exe` 打开 Codex CLI，
  双击 `excel-codex-desktop.cmd` 给桌面版用。
- **macOS（Apple 芯片）**：`excel-codex-bridge-0.4.6-macos-arm64.tar.gz`
- **macOS（Intel）**：`excel-codex-bridge-0.4.6-macos-x64.tar.gz`
- **Linux / WSL 或从源码运行**：下载 Source code，使用 `excel-codex.sh`（需要 Python 3.10+）。
- **Linux / VPS 的 SUB2API 部署**：下载本版本 Source code，按 [部署文档](https://github.com/Kaixxrua/excel-codex-bridge/blob/v0.4.6/docs/sub2api.md) 构建 `packaging/sub2api/compose.yaml`。

macOS 推荐在终端用 `curl` 下载，这样不会被"无法验证开发者"拦下（Intel 芯片把 `arm64` 换成 `x64`）：

```
curl -fL https://github.com/Kaixxrua/excel-codex-bridge/releases/download/v0.4.6/excel-codex-bridge-0.4.6-macos-arm64.tar.gz | tar xz
./excel-codex-bridge-0.4.6-macos-arm64/excel-codex status
```

用浏览器下载的，解压后先运行一次 `xattr -dr com.apple.quarantine <解压出的目录>`。

## 注意

- 安装包附带中英文 SUB2API 部署文档。
- **macOS 支持仍是实验性的**：从真实 Mac 版 Excel 读取登录态还没实机验证过，欢迎反馈。
- 程序没有代码签名（Windows 和 macOS 都没有）。可以用同目录的 `.sha256` 文件核对下载是否完整。

交流 QQ 群：966195257

---

## Changes

- **Image generation.** Ask Codex to "draw …" or "change this picture to …", in the CLI and the desktop
  app. Codex did not offer its image tool before, and with the header added by hand the request failed
  (404) because the bridge had no such endpoint. The bridge now sends it to the endpoint the Excel add-in
  draws pictures with (gpt-image-2), on the same session and with the add-in's choices.
- The launcher, desktop mode and `print-config` set the `x-openai-actor-authorization` header Codex asks
  for, so there is nothing to configure. If you added it by hand, you can remove it.
- Transparent backgrounds are not available (the add-in does not offer them either); pictures count
  against your ChatGPT plan.
- Tested end to end with the real Codex CLI 0.156.1 (Codex calls the image tool → bridge → the picture
  comes back to the conversation and is saved). The backend side follows the add-in's own requests and
  has not been tried on a real account yet; if the backend refuses, please share the reason Codex shows.

## Download

- **Windows**: `excel-codex-bridge-0.4.6-windows-x64.zip`. Double-click `excel-codex.exe` for the
  Codex CLI, or `excel-codex-desktop.cmd` for the desktop app.
- **macOS (Apple silicon)**: `excel-codex-bridge-0.4.6-macos-arm64.tar.gz`
- **macOS (Intel)**: `excel-codex-bridge-0.4.6-macos-x64.tar.gz`
- **Linux / WSL, or from source**: download the source code and use `excel-codex.sh` (Python 3.10+).
- **Linux / VPS with SUB2API**: download this release's source and follow the [deployment guide](https://github.com/Kaixxrua/excel-codex-bridge/blob/v0.4.6/docs/sub2api.en.md).

On a Mac, downloading with `curl` avoids the "developer cannot be verified" block (Intel: replace
`arm64` with `x64`):

```
curl -fL https://github.com/Kaixxrua/excel-codex-bridge/releases/download/v0.4.6/excel-codex-bridge-0.4.6-macos-arm64.tar.gz | tar xz
./excel-codex-bridge-0.4.6-macos-arm64/excel-codex status
```

If you downloaded with a browser, run `xattr -dr com.apple.quarantine <extracted folder>` once.

## Notes

- Packages include bilingual SUB2API deployment guides.
- **macOS support is still experimental**: reading the sign-in from a real Mac Excel has not been
  verified on hardware yet; feedback welcome.
- The programs are not code-signed (neither Windows nor macOS). Check downloads against the
  `.sha256` files next to them.

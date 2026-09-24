支持图片 · Pictures

> **非官方项目**，与 OpenAI、Microsoft 无关联。使用加载项后端可能违反 OpenAI 服务条款，风险自负。
> **Unofficial.** Not affiliated with OpenAI or Microsoft. Using the add-in's backend may violate OpenAI's terms; use at your own risk.

## 新功能

- **支持图片**：Codex 里贴的截图、`codex -i 图片.png`、模型用 `view_image` 看图都能用了，默认开启。
  Excel 后端只接受 OpenAI 能下载的图片链接，所以桥接这样转交：
  - 图片只放在桥接进程的内存里，不写盘，桥接退出就没了；
  - 第一次发图时，用包里自带的 cloudflared 开一条临时的 Cloudflare 隧道（不需要账号）；
  - 每张图的链接名随机、猜不到，只在带着这张图的请求发出后 5 分钟内能下载，隧道那头除了这些图什么也访问不到。

  图片经 Cloudflare 的网络送到 OpenAI，不会上传到别的地方。隧道掉线会自动重连；隧道开不起来或 OpenAI
  下载失败时，图片会换成一句说明照常发送，对话不会中断。`excel-codex image-host` 查看或切换方式
  （`local` 默认 / `off` 关闭 / `set <地址> <令牌>` 用自建图床），详见 README 的「图片」一节。
- **重启后历史工具调用原样回放**：以前桥接重启（重开桌面版窗口、`codex resume`）后，之前的工具调用只能按模板重建；
  现在原样记在状态目录的 `tool-calls.sqlite3`（仅本人可读，保留 60 天）。

## 下载

- **Windows**：`excel-codex-bridge-0.3.0-windows-x64.zip`。解压后双击 `excel-codex.exe` 打开 Codex CLI，
  双击 `excel-codex-desktop.cmd` 给桌面版用。
- **macOS（Apple 芯片）**：`excel-codex-bridge-0.3.0-macos-arm64.tar.gz`
- **macOS（Intel）**：`excel-codex-bridge-0.3.0-macos-x64.tar.gz`
- **Linux / WSL 或从源码运行**：下载 Source code，使用 `excel-codex.sh`（需要 Python 3.10+）；
  要用图片请自行安装 cloudflared。

macOS 推荐在终端用 `curl` 下载，这样不会被"无法验证开发者"拦下（Intel 芯片把 `arm64` 换成 `x64`）：

```
curl -fL https://github.com/Kaixxrua/excel-codex-bridge/releases/download/v0.3.0/excel-codex-bridge-0.3.0-macos-arm64.tar.gz | tar xz
./excel-codex-bridge-0.3.0-macos-arm64/excel-codex status
```

用浏览器下载的，解压后先运行一次 `xattr -dr com.apple.quarantine <解压出的目录>`。

## 注意

- 包里多了 Cloudflare 的 `cloudflared`（2026.9.3，Apache License 2.0，许可见 `CLOUDFLARED-LICENSE`），安装包因此变大到约 36 MB。
- 已实测：从国内服务器能开起隧道，OpenAI 能通过隧道链接取到图片并正确识别。隧道连接不走 `--proxy`，
  所在网络屏蔽 Cloudflare 隧道（出站 7844 端口）时图片会退回成文字说明，可改用自建图床。
- **macOS 支持仍是实验性的**：从真实 Mac 版 Excel 读取登录态还没实机验证过，欢迎反馈。
- 程序没有代码签名（Windows 和 macOS 都没有）。可以用同目录的 `.sha256` 文件核对下载是否完整。

交流 QQ 群：966195257

---

## What's new

- **Pictures.** Screenshots pasted into Codex, `codex -i picture.png` and the model's `view_image` now
  work, on by default. The Excel backend only takes pictures as links OpenAI can fetch, so the bridge:
  - keeps pictures only in its memory: nothing is written to disk, and they are gone when it stops;
  - with the first picture, opens a temporary Cloudflare tunnel with the bundled cloudflared (no account needed);
  - gives each picture a random, unguessable link that can be fetched only for 5 minutes after a
    request containing it is sent; nothing else is reachable through the tunnel.

  Pictures travel to OpenAI through Cloudflare's network and are not uploaded anywhere else. The
  tunnel reconnects if it drops; if it cannot open or OpenAI cannot fetch a picture, the picture is
  replaced with a short note and the request goes through anyway. `excel-codex image-host` shows or
  switches the mode (`local` default / `off` / `set <url> <token>` for your own image host); see
  Pictures in the README.
- **Earlier tool calls replay exactly after a restart.** After the bridge restarted (a reopened
  desktop window, `codex resume`), earlier tool calls used to be rebuilt from a template; they are
  now kept as sent in `tool-calls.sqlite3` in the state folder (owner-only, kept 60 days).

## Download

- **Windows**: `excel-codex-bridge-0.3.0-windows-x64.zip`. Double-click `excel-codex.exe` for the
  Codex CLI, or `excel-codex-desktop.cmd` for the desktop app.
- **macOS (Apple silicon)**: `excel-codex-bridge-0.3.0-macos-arm64.tar.gz`
- **macOS (Intel)**: `excel-codex-bridge-0.3.0-macos-x64.tar.gz`
- **Linux / WSL, or from source**: download the source code and use `excel-codex.sh` (Python 3.10+);
  install cloudflared yourself for pictures.

On a Mac, downloading with `curl` avoids the "developer cannot be verified" block (Intel: replace
`arm64` with `x64`):

```
curl -fL https://github.com/Kaixxrua/excel-codex-bridge/releases/download/v0.3.0/excel-codex-bridge-0.3.0-macos-arm64.tar.gz | tar xz
./excel-codex-bridge-0.3.0-macos-arm64/excel-codex status
```

If you downloaded it with a browser, run `xattr -dr com.apple.quarantine <extracted folder>` once.

## Notes

- The packages now include Cloudflare's `cloudflared` (2026.9.3, Apache License 2.0, see
  `CLOUDFLARED-LICENSE`), which brings them to about 36 MB.
- Tested for real: the tunnel opens from a server in mainland China, and OpenAI fetches and reads
  pictures through it. The tunnel does not use `--proxy`; on a network that blocks Cloudflare
  tunnels (outbound port 7844) pictures fall back to a note, and your own image host is the alternative.
- **macOS support is still experimental.** Reading the session from a real Mac Excel has not been
  verified on a real machine yet; feedback welcome.
- Neither the Windows nor the macOS build is code-signed. Check downloads against the `.sha256` files.

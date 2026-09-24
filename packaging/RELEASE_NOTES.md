图片中转、Docker 自建图床、减少重复消息 · Picture relay, one-command image host, fewer repeated messages

> **非官方项目**，与 OpenAI、Microsoft 无关联。使用加载项后端可能违反 OpenAI 服务条款，风险自负。
> **Unofficial.** Not affiliated with OpenAI or Microsoft. Using the add-in's backend may violate OpenAI's terms; use at your own risk.

## 新功能

- **公共图片中转（可选，默认不开）**：连不上 Cloudflare 隧道的网络，运行 `excel-codex image-host relay`
  就能改用本项目作者运行的中转 `https://img.aigcnews.cn`，什么都不用配置。本机隧道开不起来时，窗口里也会提示这条命令。
  图片会离开你的电脑，所以要你自己选。中转这样处理图片：
  - 按像素重新保存一遍，照片的拍摄地点等元数据不会留下；
  - 只给 OpenAI 的下载程序看，浏览器和其他人打不开；
  - 最后一次使用 1 小时后删除，不备份、不做别的用途（同一轮对话里 OpenAI 每次请求都会重新下载图片，所以留 1 小时）；
  - 按来源 IP 限额，防止被当成免费图床。
- **Docker 一键自建图床**：`deploy/image-host/deploy.sh img.example.com` 在自己的服务器上起一个私有图床
  （自动生成上传令牌、自动申请 https 证书）；加 `--relay` 起一个和上面一样的开放中转，
  已有 Caddy/nginx 时加 `--behind-proxy`。详见 README 的「图片」一节。

## 修复

- **减少重复显示的消息**：上游在最后一个事件前断开连接，或者模型长时间思考不出字时，Codex 会认为连接断了，
  自动重连并把这一轮再请求一次，桌面版里就会多出一段几乎一样的话。现在：
  - 所有内容都已收到、只差结束事件时，桥接替上游把这一轮收尾，Codex 不再重试；
  - 上游安静时每 15 秒发一个进行中事件，模型想得久也不会触发 Codex 5 分钟的空闲超时。

  如果仍然看到重复，请把 `bridge.log` 里 `upstream stream ended` 开头的几行和 Codex 桌面版的版本号发到 issue 或交流群。

## 下载

- **Windows**：`excel-codex-bridge-0.3.1-windows-x64.zip`。解压后双击 `excel-codex.exe` 打开 Codex CLI，
  双击 `excel-codex-desktop.cmd` 给桌面版用。
- **macOS（Apple 芯片）**：`excel-codex-bridge-0.3.1-macos-arm64.tar.gz`
- **macOS（Intel）**：`excel-codex-bridge-0.3.1-macos-x64.tar.gz`
- **Linux / WSL 或从源码运行**：下载 Source code，使用 `excel-codex.sh`（需要 Python 3.10+）；
  要用图片请自行安装 cloudflared，或用 `image-host relay`。

macOS 推荐在终端用 `curl` 下载，这样不会被"无法验证开发者"拦下（Intel 芯片把 `arm64` 换成 `x64`）：

```
curl -fL https://github.com/Kaixxrua/excel-codex-bridge/releases/download/v0.3.1/excel-codex-bridge-0.3.1-macos-arm64.tar.gz | tar xz
./excel-codex-bridge-0.3.1-macos-arm64/excel-codex status
```

用浏览器下载的，解压后先运行一次 `xattr -dr com.apple.quarantine <解压出的目录>`。

## 注意

- 默认的图片方式没变，仍是本机隧道（图片只在内存里，不经过任何第三方服务器）。
- **macOS 支持仍是实验性的**：从真实 Mac 版 Excel 读取登录态还没实机验证过，欢迎反馈。
- 程序没有代码签名（Windows 和 macOS 都没有）。可以用同目录的 `.sha256` 文件核对下载是否完整。

交流 QQ 群：966195257

---

## What's new

- **A public picture relay (optional, off by default).** On networks that cannot reach Cloudflare's
  tunnels, `excel-codex image-host relay` sends pictures through `https://img.aigcnews.cn`, a relay
  run by this project's author, with nothing to set up; the window suggests it when the local tunnel
  cannot open. Pictures then leave your computer, so it is your choice. The relay:
  - saves each picture afresh from its pixels, so no metadata (such as a photo's location) is kept;
  - serves it only to OpenAI's fetcher, not to browsers or anyone else;
  - deletes it an hour after its last use, without backups or any other use (OpenAI fetches the
    picture again for every request in a conversation, hence the hour);
  - limits uploads per address, so it cannot be used as a free image host.
- **Your own image host in one command, with Docker.** `deploy/image-host/deploy.sh img.example.com`
  starts a private image host on your server (it makes an upload token and gets an https
  certificate); `--relay` starts an open relay like the one above, and `--behind-proxy` fits in
  next to a Caddy or nginx you already run. See Pictures in the README.

## Fixes

- **Fewer repeated messages.** When the upstream dropped the connection just before its last event,
  or the model thought for a long time without output, Codex took the stream as broken, reconnected
  and asked for the turn again, and the desktop app showed a second, nearly identical message. Now:
  - when everything but the closing event has arrived, the bridge closes the turn itself and Codex
    does not retry;
  - while the upstream is quiet, the bridge sends an in-progress event every 15 seconds, so long
    thinking no longer runs into Codex's five-minute idle timeout.

  If you still see repeats, please send the lines starting with `upstream stream ended` from
  `bridge.log` and your Codex desktop app version (an issue is fine).

## Download

- **Windows**: `excel-codex-bridge-0.3.1-windows-x64.zip`. Double-click `excel-codex.exe` for the
  Codex CLI, or `excel-codex-desktop.cmd` for the desktop app.
- **macOS (Apple silicon)**: `excel-codex-bridge-0.3.1-macos-arm64.tar.gz`
- **macOS (Intel)**: `excel-codex-bridge-0.3.1-macos-x64.tar.gz`
- **Linux / WSL, or from source**: download the source code and use `excel-codex.sh` (Python 3.10+);
  install cloudflared yourself for pictures, or use `image-host relay`.

On a Mac, downloading with `curl` avoids the "developer cannot be verified" block (Intel: replace
`arm64` with `x64`):

```
curl -fL https://github.com/Kaixxrua/excel-codex-bridge/releases/download/v0.3.1/excel-codex-bridge-0.3.1-macos-arm64.tar.gz | tar xz
./excel-codex-bridge-0.3.1-macos-arm64/excel-codex status
```

If you downloaded with a browser, run `xattr -dr com.apple.quarantine <extracted folder>` once.

## Notes

- The default for pictures is unchanged: the local tunnel (pictures stay in memory and pass through
  no third-party server).
- **macOS support is still experimental**: reading the sign-in from a real Mac Excel has not been
  verified on hardware yet; feedback welcome.
- The programs are not code-signed (neither Windows nor macOS). Check downloads against the
  `.sha256` files next to them.

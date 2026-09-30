模型思考时连接不再被代理当闲置关掉 · The connection no longer gets closed as idle while the model thinks

> **非官方项目**，与 OpenAI、Microsoft 无关联。使用加载项后端可能违反 OpenAI 服务条款，风险自负。
> **Unofficial.** Not affiliated with OpenAI or Microsoft. Using the add-in's backend may violate OpenAI's terms; use at your own risk.

## 变化

- **不再在回复开始一两分钟后报 `The Excel backend closed the connection N minutes into the answer`**：模型先思考、
  一个字还没写出来时，连接上可能几分钟都没有数据，不少代理节点和路由器会把这样的连接当作闲置关掉（有的 1 分钟
  就关），后端又没办法接着上次的回答往下发，Codex 只能从头重发，还可能在同样的地方再被断开。现在桥接和后端之间
  改用 HTTP/2，回复进行中每 10 秒在连接上发一个 PING（后端会回应），路上的每一段都能看到连接还在用。
  `EXCEL_BRIDGE_UPSTREAM_PING=<秒>` 可改间隔（`1`–`60`），`0` 不发 PING 并改回 HTTP/1.1。
- 详见 README 的
  [网络中断](https://github.com/Kaixxrua/excel-codex-bridge#网络中断)。

0.5.13 起双击 `excel-codex-desktop.cmd` 会自动装上这一版；0.5.12 及更早的版本需要手动下载替换一次。

0.5.17 的变化（回复中途断开时显示原因，不再只报 `stream closed before response.completed`）见
[v0.5.17 发布说明](https://github.com/Kaixxrua/excel-codex-bridge/releases/tag/v0.5.17)；0.5.16 的变化（压缩长对话、写长工具调用时不再报 `idle timeout waiting for SSE`）见
[v0.5.16 发布说明](https://github.com/Kaixxrua/excel-codex-bridge/releases/tag/v0.5.16)；0.5.15 的变化（升级后模型菜单还是旧的时说清原因、手动 `serve` 跟着更新模型列表）见
[v0.5.15 发布说明](https://github.com/Kaixxrua/excel-codex-bridge/releases/tag/v0.5.15)；0.5.14 的变化（中转站 / Cockpit 下不再报 `exec is not a tool in the catalog`）见
[v0.5.14 发布说明](https://github.com/Kaixxrua/excel-codex-bridge/releases/tag/v0.5.14)；0.5.13 的变化（双击 `excel-codex-desktop.cmd` 自动更新）见
[v0.5.13 发布说明](https://github.com/Kaixxrua/excel-codex-bridge/releases/tag/v0.5.13)；0.5.12 的变化（子代理不再报加密内容无法解码、老会话的工具调用不再越错越多）见
[v0.5.12 发布说明](https://github.com/Kaixxrua/excel-codex-bridge/releases/tag/v0.5.12)；0.5.11 的变化（桌面版不再卡加载、网络闪断自动重连、长对话不再报请求过大、系统时区不再来回跳）见
[v0.5.11 发布说明](https://github.com/Kaixxrua/excel-codex-bridge/releases/tag/v0.5.11)；0.5.10 的变化（中转站名下的对话并到 `openai`）见
[v0.5.10 发布说明](https://github.com/Kaixxrua/excel-codex-bridge/releases/tag/v0.5.10)；0.5.9 的变化（普通版到 450k 才压缩、迁移过的对话关掉桥接后能接着聊）见
[v0.5.9 发布说明](https://github.com/Kaixxrua/excel-codex-bridge/releases/tag/v0.5.9)；0.5.8 的变化（被限流时最多等 5 分钟）见
[v0.5.8 发布说明](https://github.com/Kaixxrua/excel-codex-bridge/releases/tag/v0.5.8)；0.5.7 的变化（限流等待、
旧的桥接对话启动时自动并进共享列表）见
[v0.5.7 发布说明](https://github.com/Kaixxrua/excel-codex-bridge/releases/tag/v0.5.7)；0.5.4 的新功能
（和官方链路会话互通、指定生图模型、出口时区）见
[v0.5.4 发布说明](https://github.com/Kaixxrua/excel-codex-bridge/releases/tag/v0.5.4)。

## 下载

- **Windows**：`excel-codex-bridge-0.5.18-windows-x64.zip`。解压后双击 `excel-codex.exe` 打开 Codex CLI，
  双击 `excel-codex-desktop.cmd` 给桌面版用。
- **macOS（Apple 芯片）**：`excel-codex-bridge-0.5.18-macos-arm64.tar.gz`
- **macOS（Intel）**：`excel-codex-bridge-0.5.18-macos-x64.tar.gz`
- **Linux / WSL 或从源码运行**：下载 Source code，使用 `excel-codex.sh`（需要 Python 3.10+）。
- **Linux / VPS 的 SUB2API 部署**：下载本版本 Source code，按 [部署文档](https://github.com/Kaixxrua/excel-codex-bridge/blob/v0.5.18/docs/sub2api.md) 构建 `packaging/sub2api/compose.yaml`。

macOS 推荐在终端用 `curl` 下载，这样不会被"无法验证开发者"拦下（Intel 芯片把 `arm64` 换成 `x64`）：

```
curl -fL https://github.com/Kaixxrua/excel-codex-bridge/releases/download/v0.5.18/excel-codex-bridge-0.5.18-macos-arm64.tar.gz | tar xz
./excel-codex-bridge-0.5.18-macos-arm64/excel-codex status
```

用浏览器下载的，解压后先运行一次 `xattr -dr com.apple.quarantine <解压出的目录>`。

## 注意

- 安装包附带中英文 SUB2API 部署文档。
- **macOS 支持仍是实验性的**：从真实 Mac 版 Excel 读取登录态还没实机验证过，欢迎反馈。
- 程序没有代码签名（Windows 和 macOS 都没有）。可以用同目录的 `.sha256` 文件核对下载是否完整。

交流 QQ 群：966195257

---

## Changes

- **No more `The Excel backend closed the connection N minutes into the answer` a minute or two into an
  answer**: while the model thinks, before it writes a word, the connection can carry nothing for
  minutes, and many proxy nodes and routers close such a connection as idle (some after a minute). The
  backend cannot carry on with that answer, so Codex started over, at times only to be cut off at the
  same point again. The bridge now talks HTTP/2 to the backend and, while an answer is coming, sends a
  PING on the connection every 10 seconds (the backend answers it), so every hop on the way sees it in
  use. `EXCEL_BRIDGE_UPSTREAM_PING=<seconds>` sets the interval (`1` to `60`); `0` sends none and goes
  back to HTTP/1.1.
- See [Network drops](https://github.com/Kaixxrua/excel-codex-bridge/blob/main/README.en.md#network-drops)
  in the README.

From 0.5.13, double-clicking `excel-codex-desktop.cmd` installs this release by itself; 0.5.12 and
earlier need it downloaded and replaced by hand once.

For 0.5.17's changes (Codex saying why an answer broke off instead of just `stream closed before
response.completed`), see the [v0.5.17 release notes](https://github.com/Kaixxrua/excel-codex-bridge/releases/tag/v0.5.17);
for 0.5.16's changes (no more `idle timeout waiting for SSE` during long compactions and long tool
calls), see the [v0.5.16 release notes](https://github.com/Kaixxrua/excel-codex-bridge/releases/tag/v0.5.16);
for 0.5.15's changes (saying why the model menu is an old one after an update, manual `serve` updating
the model list), see the
[v0.5.15 release notes](https://github.com/Kaixxrua/excel-codex-bridge/releases/tag/v0.5.15); for 0.5.14's changes (no more `exec is not a tool in the catalog` through relays and Cockpit), see the
[v0.5.14 release notes](https://github.com/Kaixxrua/excel-codex-bridge/releases/tag/v0.5.14); for 0.5.13's changes (`excel-codex-desktop.cmd` updating itself), see the
[v0.5.13 release notes](https://github.com/Kaixxrua/excel-codex-bridge/releases/tag/v0.5.13); for 0.5.12's changes (subagents getting their task as text, older conversations no longer failing tool
calls more and more), see the
[v0.5.12 release notes](https://github.com/Kaixxrua/excel-codex-bridge/releases/tag/v0.5.12); for 0.5.11's changes (no more stuck loading, reconnects after network drops, long conversations
fitting, the system timezone holding still), see the
[v0.5.11 release notes](https://github.com/Kaixxrua/excel-codex-bridge/releases/tag/v0.5.11); for 0.5.10's changes (a relay's conversations moving under `openai`), see the
[v0.5.10 release notes](https://github.com/Kaixxrua/excel-codex-bridge/releases/tag/v0.5.10); for 0.5.9's changes (standard models compacting at 450k, moved conversations carrying on with the
bridge off), see the [v0.5.9 release notes](https://github.com/Kaixxrua/excel-codex-bridge/releases/tag/v0.5.9); for
0.5.8's (up to 5 minutes' wait under the rate limit), the
[v0.5.8 release notes](https://github.com/Kaixxrua/excel-codex-bridge/releases/tag/v0.5.8); for 0.5.7's
(the rate-limit wait, the bridge's earlier conversations moved into the shared list at start), the
[v0.5.7 release notes](https://github.com/Kaixxrua/excel-codex-bridge/releases/tag/v0.5.7); for what 0.5.4
added (conversations shared with the official sign-in, choosing the image model, the exit timezone),
the [v0.5.4 release notes](https://github.com/Kaixxrua/excel-codex-bridge/releases/tag/v0.5.4).

## Download

- **Windows**: `excel-codex-bridge-0.5.18-windows-x64.zip`. Double-click `excel-codex.exe` for the
  Codex CLI, or `excel-codex-desktop.cmd` for the desktop app.
- **macOS (Apple silicon)**: `excel-codex-bridge-0.5.18-macos-arm64.tar.gz`
- **macOS (Intel)**: `excel-codex-bridge-0.5.18-macos-x64.tar.gz`
- **Linux / WSL, or from source**: download the source code and use `excel-codex.sh` (Python 3.10+).
- **Linux / VPS with SUB2API**: download this release's source and follow the [deployment guide](https://github.com/Kaixxrua/excel-codex-bridge/blob/v0.5.18/docs/sub2api.en.md).

On a Mac, downloading with `curl` avoids the "developer cannot be verified" block (Intel: replace
`arm64` with `x64`):

```
curl -fL https://github.com/Kaixxrua/excel-codex-bridge/releases/download/v0.5.18/excel-codex-bridge-0.5.18-macos-arm64.tar.gz | tar xz
./excel-codex-bridge-0.5.18-macos-arm64/excel-codex status
```

If you downloaded with a browser, run `xattr -dr com.apple.quarantine <extracted folder>` once.

## Notes

- Packages include bilingual SUB2API deployment guides.
- **macOS support is still experimental**: reading the sign-in from a real Mac Excel has not been
  verified on hardware yet; feedback welcome.
- The programs are not code-signed (neither Windows nor macOS). Check downloads against the
  `.sha256` files next to them.

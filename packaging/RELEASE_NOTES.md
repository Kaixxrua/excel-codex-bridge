提示词瘦身 · A smaller prompt

> **非官方项目**，与 OpenAI、Microsoft 无关联。使用加载项后端可能违反 OpenAI 服务条款，风险自负。
> **Unofficial.** Not affiliated with OpenAI or Microsoft. Using the add-in's backend may violate OpenAI's terms; use at your own risk.

## 变化

- **提示词小了很多**：
  - 模型目录打开了 Codex 的 `tool_search`（官方模型目录也是这样设的）。ChatGPT 登录带上的应用（GitHub、Gmail……）
    和你配的 MCP 服务器，它们的工具不再每个请求全部写进提示词，模型要用时先搜再调用。开着应用时，
    每个请求桥接写给后端的提示词从约 30 万字符降到约 3.3 万（桌面版约 34 万降到约 4.9 万）。
  - 桌面版自带的 `codex_app` 工具（31 个）和直接发来的插件 / MCP 工具只写摘要和参数类型；模型传错参数时，
    桥接把完整定义连同错误原因告诉它。没开应用的桌面版每个请求约 6.6 万字符降到约 4.6 万。
  - 工具定义里只给校验程序看的字段不再写进提示词，目录后面的提醒不再重复列出所有工具名。
  - Excel 后端自带的约 2.2 万 token 前缀由后端加，桥接去不掉。
- 详见 README 的[提示词大小](https://github.com/Kaixxrua/excel-codex-bridge#提示词大小)。
- **菜单里没有 `ultra`**：0.5.20 的 `ultra` 要桥接重写模型目录后才出现。还在跑旧版桥接的（比如自己装成服务的），
  升级到这一版并重启桥接，再完全退出 Codex 重新打开。桌面版通过 SSH 连另一台机器时，还要在那台机器上运行
  `codex app-server daemon restart`：那里常驻的 app-server 只在启动时读一次模型目录。README 的
  [Ultra](https://github.com/Kaixxrua/excel-codex-bridge#ultra) 写明了。

0.5.13 起双击 `excel-codex-desktop.cmd` 会自动装上这一版；0.5.12 及更早的版本需要手动下载替换一次。

0.5.20 的变化（Codex 的 ultra 推理强度）见
[v0.5.20 发布说明](https://github.com/Kaixxrua/excel-codex-bridge/releases/tag/v0.5.20)；0.5.19 的变化（双击恢复官方 Codex 配置）见
[v0.5.19 发布说明](https://github.com/Kaixxrua/excel-codex-bridge/releases/tag/v0.5.19)；0.5.18 的变化（模型思考时连接不再被代理当闲置关掉）见
[v0.5.18 发布说明](https://github.com/Kaixxrua/excel-codex-bridge/releases/tag/v0.5.18)；0.5.17 的变化（回复中途断开时显示原因，不再只报 `stream closed before response.completed`）见
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

- **Windows**：`excel-codex-bridge-0.5.21-windows-x64.zip`。解压后双击 `excel-codex.exe` 打开 Codex CLI，
  双击 `excel-codex-desktop.cmd` 给桌面版用，双击 `excel-codex-restore.cmd` 恢复官方 Codex 配置。
- **macOS（Apple 芯片）**：`excel-codex-bridge-0.5.21-macos-arm64.tar.gz`
- **macOS（Intel）**：`excel-codex-bridge-0.5.21-macos-x64.tar.gz`
- **Linux / WSL 或从源码运行**：下载 Source code，使用 `excel-codex.sh`（需要 Python 3.10+）。
- **Linux / VPS 的 SUB2API 部署**：下载本版本 Source code，按 [部署文档](https://github.com/Kaixxrua/excel-codex-bridge/blob/v0.5.21/docs/sub2api.md) 构建 `packaging/sub2api/compose.yaml`。

macOS 推荐在终端用 `curl` 下载，这样不会被"无法验证开发者"拦下（Intel 芯片把 `arm64` 换成 `x64`）：

```
curl -fL https://github.com/Kaixxrua/excel-codex-bridge/releases/download/v0.5.21/excel-codex-bridge-0.5.21-macos-arm64.tar.gz | tar xz
./excel-codex-bridge-0.5.21-macos-arm64/excel-codex status
```

用浏览器下载的，解压后先运行一次 `xattr -dr com.apple.quarantine <解压出的目录>`。

## 注意

- 安装包附带中英文 SUB2API 部署文档。
- **macOS 支持仍是实验性的**：从真实 Mac 版 Excel 读取登录态还没实机验证过，欢迎反馈。
- 程序没有代码签名（Windows 和 macOS 都没有）。可以用同目录的 `.sha256` 文件核对下载是否完整。

交流 QQ 群：966195257

---

## Changes

- **A much smaller prompt**:
  - The model entries turn on Codex's `tool_search` (as OpenAI's own entries do). The tools of the apps
    a ChatGPT sign-in brings along (GitHub, Gmail, ...) and of your MCP servers no longer go into every
    request in full; the model searches for one when it needs it, then calls it. With apps on, the
    prompt the bridge writes for the backend drops from about 300k characters to about 33k per request
    (desktop: about 340k to about 49k).
  - The desktop app's own `codex_app` tools (31 of them) and plugin or MCP tools sent directly are
    summarized, with each parameter's type; when the model passes the wrong parameters, the bridge
    tells it why along with the full definition. The desktop app without apps drops from about 66k
    characters to about 46k per request.
  - Fields in tool definitions that only a validator reads are left out, and the reminder after the
    catalog no longer lists every tool name again.
  - The ~22k-token prefix the Excel backend adds itself cannot be removed by the bridge.
- See [Prompt size](https://github.com/Kaixxrua/excel-codex-bridge/blob/main/README.en.md#prompt-size) in the README.
- **No `ultra` in the menu**: 0.5.20's `ultra` appears once the bridge rewrites its model entries. If an
  older bridge is still running (one installed as a service, say), update it to this release and
  restart it, then quit Codex fully and open it again. When the desktop app connects to another
  machine over SSH, also run `codex app-server daemon restart` there: the app-server that stays
  running there reads the model entries only when it starts. See
  [Ultra](https://github.com/Kaixxrua/excel-codex-bridge/blob/main/README.en.md#ultra) in the README.

From 0.5.13, double-clicking `excel-codex-desktop.cmd` installs this release by itself; 0.5.12 and
earlier need it downloaded and replaced by hand once.

For 0.5.20's changes (Codex's ultra reasoning effort), see the
[v0.5.20 release notes](https://github.com/Kaixxrua/excel-codex-bridge/releases/tag/v0.5.20);
for 0.5.19's changes (a double-click that puts Codex back on its own setup), see the
[v0.5.19 release notes](https://github.com/Kaixxrua/excel-codex-bridge/releases/tag/v0.5.19);
for 0.5.18's changes (the connection no longer closed as idle while the model thinks), see the
[v0.5.18 release notes](https://github.com/Kaixxrua/excel-codex-bridge/releases/tag/v0.5.18);
for 0.5.17's changes (Codex saying why an answer broke off instead of just `stream closed before
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

- **Windows**: `excel-codex-bridge-0.5.21-windows-x64.zip`. Double-click `excel-codex.exe` for the
  Codex CLI, `excel-codex-desktop.cmd` for the desktop app, or `excel-codex-restore.cmd` to put Codex
  back on its own setup.
- **macOS (Apple silicon)**: `excel-codex-bridge-0.5.21-macos-arm64.tar.gz`
- **macOS (Intel)**: `excel-codex-bridge-0.5.21-macos-x64.tar.gz`
- **Linux / WSL, or from source**: download the source code and use `excel-codex.sh` (Python 3.10+).
- **Linux / VPS with SUB2API**: download this release's source and follow the [deployment guide](https://github.com/Kaixxrua/excel-codex-bridge/blob/v0.5.21/docs/sub2api.en.md).

On a Mac, downloading with `curl` avoids the "developer cannot be verified" block (Intel: replace
`arm64` with `x64`):

```
curl -fL https://github.com/Kaixxrua/excel-codex-bridge/releases/download/v0.5.21/excel-codex-bridge-0.5.21-macos-arm64.tar.gz | tar xz
./excel-codex-bridge-0.5.21-macos-arm64/excel-codex status
```

If you downloaded with a browser, run `xattr -dr com.apple.quarantine <extracted folder>` once.

## Notes

- Packages include bilingual SUB2API deployment guides.
- **macOS support is still experimental**: reading the sign-in from a real Mac Excel has not been
  verified on hardware yet; feedback welcome.
- The programs are not code-signed (neither Windows nor macOS). Check downloads against the
  `.sha256` files next to them.

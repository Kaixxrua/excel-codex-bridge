# excel-codex-bridge

**简体中文** | [English](README.en.md)

> **非官方项目**，与 OpenAI、Microsoft 无任何关联，也未获其认可。使用前请先读[风险与免责声明](#风险与免责声明)。

用你**自己电脑上**的 ChatGPT Excel 加载项会话来跑 [Codex CLI](https://github.com/openai/codex)
和 Codex 桌面版：双击或一条命令，本地桥接和 Codex 一起启动，退出时桥接自动关闭。
需要登录时自动替你打开 Excel 的 ChatGPT 面板，登录由加载项自己完成；
不监听本机以外的地址，会话只在内存里，只发往 OpenAI。

## 工作原理

```
Codex CLI ──(Responses API, 127.0.0.1)──▶ excel-codex-bridge ──(HTTPS)──▶ bps.openai.com
                                                 ▲                      （ChatGPT for Excel 后端）
                              只读 Excel 加载项本地缓存的登录态（不落盘）
```

- **自己不做登录**：不实现 OAuth，不碰账号密码。登录只在 Excel 的 ChatGPT 面板里完成
  （需要时本工具会[自动打开这个面板](#自动登录windows)），本工具只读取加载项在本机 WebView2
  存储里缓存的那份会话（`bps_auth_tokens`）。
- **token 只在内存里**：不写盘、不上传，只随请求发往 `bps.openai.com`；日志只有请求行和错误类型，
  不含提示词和 token。
- **只给本机用**：只监听回环地址；拒绝非回环来源、非回环 `Host`（防 DNS rebinding）
  以及任何带 `Origin` 头的浏览器请求。`serve` 拒绝绑定 `0.0.0.0`。
- **工具调用**：Excel 后端不接受客户端自带工具。桥接把 Codex 的工具（shell、apply_patch…）
  写进提示词，模型通过后端原生的 `run_officejs` 回传调用，桥接再还原成 Codex 的工具调用。
- **图片**：图片和请求一样只发给 OpenAI。后端不收内嵌图片的地方，桥接像加载项的「上传文件」按钮
  那样把图片传到 OpenAI 再引用，见[图片](#图片)。
- **不乱改你的 Codex 配置**：命令行启动器用 `-c` 参数临时指定 provider 和模型目录，
  `~/.codex/config.toml` 保持原样。只有[桌面版模式](#在-codex-桌面版--ide-插件中使用)会改它，
  窗口关闭即按原样恢复，并留有备份。

## 前提

- Windows 10/11 + **Microsoft 365 桌面版 Excel**，以及 ChatGPT 加载项（发布者 OpenAI）。
  Mac 用户见 [macOS](#macos--wsl实验性)。
  不用提前登录，第一次运行时会自动打开 Excel 带你登录；还没安装加载项的，Excel 一般会提示信任并安装，
  不行就先从 开始 → 加载项 里装上。你的 ChatGPT 套餐需要能用这个加载项。
- Codex CLI：`npm install -g @openai/codex`。
- 只有从源码运行才需要 Python 3.10+（[python.org](https://www.python.org/downloads/)，安装时勾选 *Add python.exe to PATH*），免安装版不需要。
- 能访问 `bps.openai.com`（需要代理见[代理](#代理)）。

## 快速开始（Windows）

**方式一：免安装版（推荐）**

1. 从 [Releases](https://github.com/Kaixxrua/excel-codex-bridge/releases/latest) 下载
   `excel-codex-bridge-<版本>-windows-x64.zip`，解压到任意目录，例如 `D:\tools\excel-codex-bridge`。
2. 双击 `excel-codex.exe` 就会打开 Codex，工作目录是你的用户目录。
   要在某个项目里用，就在项目目录打开终端运行 `D:\tools\excel-codex-bridge\excel-codex.exe`，
   或者把解压目录加进 `PATH`，之后直接输入 `excel-codex`。

exe 没有代码签名，首次运行时 SmartScreen 可能拦一下，点"更多信息 → 仍要运行"即可。

第一次运行时还没有登录，程序会自动打开 Excel 并弹出 ChatGPT 面板，在面板里登录即可，
登录完 Excel 会自动关掉，Codex 随即启动。详见[自动登录](#自动登录windows)。

**方式二：从源码运行**

把本仓库 clone 到任意目录，在项目目录运行其中的 `excel-codex.cmd`（或者双击它）。
首次运行会在仓库目录建 `.venv` 并安装依赖，之后秒开。

常用写法（`--` 之后的参数原样交给 Codex）：

```
excel-codex                                   交互式 Codex，默认 gpt-5.6-sol-excel
excel-codex --model gpt-5.6-terra-excel       换模型
excel-codex -- -c model_reasoning_effort=high 调推理强度
excel-codex -- exec "给 README 加一个目录"      非交互执行
excel-codex -- resume --last                  继续上次会话
excel-codex status                            检查会话是否可用、何时过期
excel-codex login                             打开 Excel 的 ChatGPT 面板登录或续期
excel-codex desktop                           让 Codex 桌面版 / IDE 插件走 Excel 链路
```

启动器自己的选项：`--model`、`--proxy`、`--port`、`--codex <路径>`、`--webview-dir <目录>`、
`--skip-session-check`、`--no-auto-signin`。

## 模型

| Codex 里选择 | 上游模型 | 上下文 |
| --- | --- | --- |
| `gpt-5.6-sol-excel`（默认） | `gpt-5.6-sol` | 272k |
| `gpt-5.6-terra-excel` | `gpt-5.6-terra` | 272k |
| `gpt-5.6-luna-excel` | `gpt-5.6-luna` | 200k |
| `gpt-6-astra-excel` | `gpt-6-astra` | 按 272k 处理 |

推理强度 `low` / `medium` / `high` / `xhigh`，默认 `medium`。

想试别的上游模型时，可以设置 `GHCP_EXCEL_UPSTREAM_MODEL=<上游模型名>`，把所有请求强制发给那个模型。

## 代理

桥接访问 `bps.openai.com` 的出站：

- `--proxy http://127.0.0.1:7890`（也支持 `socks5h://…`），或环境变量 `EXCEL_BRIDGE_PROXY`；
- 都没设时跟随 `HTTPS_PROXY` 和系统代理。

TLS 证书校验始终开启。Codex 到桥接走 `127.0.0.1`，启动器会自动把它加进 `NO_PROXY`。

## 自动登录（Windows）

本工具自己不登录，而是替你把 Excel 里官方的 ChatGPT 面板打开：

1. 发现本机没有可用会话时，程序生成一个小工作簿（`%LOCALAPPDATA%\excel-codex-bridge\excel-codex-sign-in.xlsx`），
   里面嵌入了 ChatGPT 加载项（应用商店编号 `WA200010215`），用 Excel 打开后面板会自动弹出。
2. 第一次会提示信任 / 安装加载项，同意后在面板里登录。如果面板没弹出来，点 开始 → 加载项 → ChatGPT。
3. 程序读到新会话后自动关闭这个工作簿；Excel 是它打开的、又没有别的工作簿时，Excel 也一起关掉。

会话还剩不到 24 小时时，运行中的桥接会在后台把 Excel 最小化打开，已登录的面板通常会自己续期，
续完自动关掉，不用你操作。`excel-codex login` 可随时手动触发（`--force` 即使会话还好也打开面板）。
不想让程序动 Excel：加 `--no-auto-signin`，或设置环境变量 `EXCEL_BRIDGE_AUTO_SIGNIN=0`。

> 自动弹出面板依赖 Office 的"随文档打开加载项"机制，是 v0.2.1 新加的，还没在各种 Office 版本上验证过，
> 个别版本或组织策略下可能不生效。这时按第 2 步手动点开面板即可，其余步骤照常自动完成。

## 在 Codex 桌面版 / IDE 插件中使用

桌面版和 IDE 插件没法传 `-c`，只读 `~/.codex/config.toml`。本工具可以临时改写它：

1. 双击免安装包里的 **`excel-codex-desktop.cmd`**（或运行 `excel-codex desktop`），
   它会检查会话（需要时自动登录）、把 `config.toml` 指向桥接，并在 `127.0.0.1:8765` 上运行桥接。
2. **重启 Codex 桌面版**（IDE 插件则重新加载窗口），模型列表里就是 `*-excel` 模型。
3. 用的时候保持这个窗口开着（可以最小化）。**关掉窗口或按 Ctrl+C，`config.toml` 按原样恢复。**

细节：

- 改写前会把原文件存成 `config.toml.before-excel-codex`；你原来的 `model`、`model_provider`
  等行只是加注释停用，恢复时逐字节还原。
- `config.toml` 是所有 Codex 客户端共用的，窗口开着期间在终端直接运行 `codex` 也会走 Excel 链路。
- 想长期保持：`excel-codex desktop --keep-config`，之后用 `excel-codex desktop --off` 恢复
  （窗口意外被杀、配置没还原时也用它）。
- 换模型：`excel-codex desktop --model gpt-5.6-terra-excel`；换端口：`--port`。

也可以全手动：`excel-codex serve` 常驻桥接，再把 `excel-codex print-config` 输出的片段加进
`config.toml`，不用时删掉。

## macOS / WSL（实验性）

**macOS 免安装版**（读取 Mac 版 Excel 沙盒里的 WebKit 本地存储，不需要 Windows 电脑）：

1. 在 Mac 版 Excel 里点 开始 → 加载项 → ChatGPT，在面板里登录一次。Mac 上没有自动登录，
   会话大约 10 天有效，到期后再打开一次面板即可，不用重启桥接。
2. 从 [Releases](https://github.com/Kaixxrua/excel-codex-bridge/releases/latest) 下载
   `excel-codex-bridge-<版本>-macos-arm64.tar.gz`（Intel 芯片选 `macos-x64`），双击解压。
3. 程序没有 Apple 签名，用浏览器下载的要先在终端解除隔离一次，否则会提示"无法验证开发者"：

   ```
   xattr -dr com.apple.quarantine ~/Downloads/excel-codex-bridge-<版本>-macos-arm64
   ```

   （用 `curl` 下载的没有隔离标记，可以跳过这一步，发布说明里有现成命令。）
4. 用法和 Windows 一样：
   - Codex CLI：在项目目录运行 `<解压目录>/excel-codex`，参数同上；也可以把解压目录加进 `PATH`。
   - Codex 桌面版：双击 `excel-codex-desktop.command`，然后按 `Cmd+Q` 完全退出 Codex 桌面版再打开。
     终端窗口保持开着，用完关掉窗口或按 Ctrl+C，`config.toml` 按原样恢复。

第一次读取会话时，macOS 可能询问是否允许"终端"访问其他 App 的数据，选允许。
想从源码运行就用 `./excel-codex.sh`（需要 Python 3.10+），桌面版模式是 `./excel-codex.sh desktop`。

**WSL**：Excel 装在 Windows 侧，把它的数据目录指给桥接：

  ```
  ./excel-codex.sh --webview-dir /mnt/c/Users/<你>/AppData/Local/Microsoft/Office
  ```

## 会话过期

加载项的 token 大约 10 天有效。桥接每次请求前都会检查，过期或临近过期时重新读取本机缓存；
在 Windows 上还会在剩余不到 24 小时时[自动打开面板续期](#自动登录windows)，**不用重启**桥接或 Codex。
`excel-codex status` 可查看剩余时间。

## 图片

Codex 里贴的截图、`codex -i 图片.png` 和模型用 `view_image` 看图都能用，不需要任何设置。桥接这样转交：

- **能内嵌就内嵌**：图片按 Codex 发来的样子（`data:` 内嵌）随请求发出，不另外上传；
- **不收内嵌时才上传**：Excel 后端不收内嵌图片的地方（目前是用户消息里的图），桥接像 Excel
  加载项里的「上传文件」按钮那样，用同一个登录会话把图片传到 OpenAI 的附件接口
  （`bps.openai.com/basispoints/api/attachments`），请求里改用返回的文件 ID。哪里不收是桥接自己
  试出来的：第一次被拒就改为上传并记住，之后同类位置的图直接上传；
- **同一张图只传一次**：本次运行里后续请求直接复用文件 ID（按账号区分，只记在内存里）；
- 上传失败或后端仍不接受时，桥接把图片换成一句说明再发送，对话不会中断，原因写在窗口或
  `bridge.log` 里。

图片和请求的其他内容一样只发往 `bps.openai.com`，不经过任何第三方，也不需要隧道、图床或别的配置；
上传和请求走同一套网络设置（包括 `--proxy`）。上传的图片和你在加载项里上传的文件一样由 OpenAI 保存。

## 限制

- 不支持并行工具调用，工具一次调一个。
- 只实现 Responses API，没有 `/responses/compact` 端点。
- Excel 后端会给每个请求加上约 2.2 万 token 的固定前缀，大部分命中缓存；额度按你的 ChatGPT 套餐计算。
- 依赖 Excel 加载项的非公开后端，OpenAI 一调整就可能失效。

## 风险与免责声明

- 这是**非官方**项目，未获 OpenAI 或 Microsoft 认可。在 Excel 以外的场景使用加载项后端，
  可能违反 OpenAI 的服务条款，导致账号受限或封禁。**风险自负**。
- 只用于**你自己的账号、你自己的电脑**。不要把桥接暴露到网络，不要共享给他人，不要拿来做中转或转售。
  本项目有意不支持这些用法：没有多用户、没有 API key 管理、不监听非回环地址。
- 本工具读取的是登录凭据。读取只发生在本机，凭据只在内存中使用，只发送到 `bps.openai.com`。
  使用前请自行审阅代码。

## 环境变量

| 变量 | 作用 |
| --- | --- |
| `EXCEL_BRIDGE_PROXY` | 出站代理（同 `--proxy`） |
| `EXCEL_BRIDGE_HOME` | 状态目录：模型目录 JSON、`bridge.log`、登录用工作簿，以及让重启后仍能原样回放历史工具调用的 `tool-calls.sqlite3`（保留 60 天）。默认 `%LOCALAPPDATA%\excel-codex-bridge` 或 `~/.excel-codex-bridge` |
| `EXCEL_BRIDGE_AUTO_SIGNIN` | 设为 `0` 时不自动打开 Excel（同 `--no-auto-signin`） |
| `CODEX_HOME` | Codex 配置目录，`desktop` 改写其中的 `config.toml`。默认 `~/.codex` |
| `GHCP_EXCEL_WEBVIEW2_DATA_DIR` | Windows WebView2 数据根目录（同 `--webview-dir`） |
| `GHCP_EXCEL_WEBKIT_WEBSITE_DATA_DIR` | macOS WebKit 数据目录 |
| `GHCP_EXCEL_RESPONSES_URL` | 上游地址（测试用） |
| `GHCP_EXCEL_UPSTREAM_MODEL` | 强制指定上游模型名 |

`GHCP_EXCEL_*` 沿用 ghcp_proxy 的命名，方便从它迁移过来。

## 开发

```
python -m venv .venv
.venv/bin/pip install -r requirements.txt pytest     # Windows: .venv\Scripts\pip
PYTHONPATH=src .venv/bin/python -m pytest
```

## 致谢与许可

Excel 会话读取和 Basispoints 协议适配代码提取自
[Nonary/ghcp_proxy](https://github.com/Nonary/ghcp_proxy)（Unlicense，基于 commit `ad23ce2`，
原许可见 [UPSTREAM-LICENSE](UPSTREAM-LICENSE)）。本项目同样以 [Unlicense](LICENSE) 发布。

感谢 [LINUX DO](https://linux.do) 社区。

## 交流群

QQ 群「Vibe coding 交流」，群号 **966195257**，扫码加入：

<img src="docs/qq-group.jpg" alt="QQ 群 966195257 二维码" width="240">

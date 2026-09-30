# excel-codex-bridge

**简体中文** | [English](README.en.md)

> **非官方项目**，与 OpenAI、Microsoft 无任何关联，也未获其认可。使用前请先读[风险与免责声明](#风险与免责声明)。

> **新增可选：SUB2API 插件。** 将 Excel 会话作为独立上游接入你自己的 SUB2API，
> 无需修改 SUB2API 源码，见 [Docker 部署与 SSH 会话同步](docs/sub2api.md)。
> 此模式会在你显式运行同步命令后，把会话送到你指定的可信服务器；
> 下文“仅本机、只发往 OpenAI”的说明指默认本地模式，不适用于远端插件模式。

用你**自己电脑上**已有的 ChatGPT 登录来跑 [Codex CLI](https://github.com/openai/codex)
和 Codex 桌面版：双击或一条命令，本地桥接和 Codex 一起启动，退出时桥接自动关闭。
默认优先用 Codex 自己的登录（`codex login`），这样不必安装 Excel；后端不接受它时自动回退到
Excel 加载项缓存的那份会话，需要时会替你打开 Excel 的 ChatGPT 面板登录。见[登录方式](#登录方式)。
不监听本机以外的地址，会话只在内存里，只发往 OpenAI。

## 工作原理

```
Codex CLI ──(Responses API, 127.0.0.1)──▶ excel-codex-bridge ──(HTTPS)──▶ bps.openai.com
                                                 ▲                      （ChatGPT for Excel 后端）
                       只读本机已有的 ChatGPT 登录（Codex 自己的，或 Excel 加载项缓存的；不落盘）
```

- **自己不做登录**：不实现 OAuth，不碰账号密码，也从不刷新或写入任何 token。它只读取本机上
  已经存在的 ChatGPT 登录——优先是 Codex 自己的（`codex login` 写在 `~/.codex/auth.json`），
  其次是 Excel 加载项缓存在 WebView2 里的那份会话（`bps_auth_tokens`）。用 Excel 那份时，需要登录会在
  面板里完成（本工具会[自动打开面板](#自动登录windows)）。取哪一份、怎么回退见[登录方式](#登录方式)。
- **token 只在内存里**：不写盘、不上传，只随请求发往 `bps.openai.com`；日志只有请求行和错误类型，
  不含提示词和 token。
- **只给本机用**：只监听回环地址；拒绝非回环来源、非回环 `Host`（防 DNS rebinding）
  以及任何带 `Origin` 头的浏览器请求。`serve` 拒绝绑定 `0.0.0.0`。
- **工具调用**：Excel 后端不接受客户端自带工具。桥接把 Codex 的工具（shell、apply_patch…）
  写进提示词，模型通过后端原生的 `run_officejs` 回传调用，桥接再还原成 Codex 的工具调用。
  互不依赖的调用（比如同时读几个文件、跑几条命令）模型会一次发出，Codex 同时执行；改文件仍按顺序来。
- **多个会话同时用**：几个 Codex 窗口、桌面版对话或子代理可以共用一个桥接同时工作，互不排队。
- **图片**：图片和请求一样只发给 OpenAI。后端不收内嵌图片的地方，桥接像加载项的「上传文件」按钮
  那样把图片传到 OpenAI 再引用，见[图片](#图片)。
- **生图**：Codex 自带的生图工具由桥接转给 Excel 加载项自己生图用的接口（gpt-image-2），
  同一个登录会话，见[生图](#生图)。
- **不乱改你的 Codex 配置**：命令行启动器用 `-c` 参数临时指定 provider 和模型目录，
  `~/.codex/config.toml` 保持原样。只有[桌面版模式](#在-codex-桌面版--ide-插件中使用)会改它，
  窗口关闭即按原样恢复，并留有备份。

## 前提

- Codex CLI：`npm install -g @openai/codex`。
- 一份本机已有的 ChatGPT 登录，二选一（详见[登录方式](#登录方式)）：
  - **Codex 自己的登录**（默认、无需 Excel）：`codex login` 用 ChatGPT 登录一次即可，任意系统都行。
  - **Excel 加载项的会话**（回退方案）：Windows 10/11 + **Microsoft 365 桌面版 Excel** 及 ChatGPT
    加载项（发布者 OpenAI），Mac 见 [macOS](#macos--wsl实验性)。不用提前登录，需要时会自动打开 Excel
    带你登录；还没装加载项的，Excel 一般会提示信任并安装，不行就从 开始 → 加载项 里装上。
    你的 ChatGPT 套餐需要能用这个加载项。
- 只有从源码运行才需要 Python 3.10+（[python.org](https://www.python.org/downloads/)，安装时勾选 *Add python.exe to PATH*），免安装版不需要。
- 能访问 `bps.openai.com`（需要代理见[代理](#代理)）。

## 登录方式

桥接需要一份本机已有的 ChatGPT 登录。可用两处，默认按顺序自动选：

1. **Codex 自己的登录**（`codex login` 写在 `$CODEX_HOME/auth.json`，默认 `~/.codex/auth.json`）。
   用这份时不必安装 Excel，任意系统都行。
2. **Excel 加载项缓存的会话**（回退）。当 Codex 没有用 ChatGPT 登录、它的登录已过期，或后端拒绝它时，
   自动改用这份；需要登录会[自动打开 Excel 面板](#自动登录windows)。

用 `--login` 或环境变量 `EXCEL_BRIDGE_LOGIN` 指定：

| 值 | 含义 |
| --- | --- |
| `auto`（默认） | 先试 Codex 的登录，不行再用 Excel 的 |
| `codex` | 只用 Codex 的登录（不碰 Excel） |
| `excel` | 只用 Excel 加载项的会话（老用户的原有行为） |

- **回退时机**（仅 `auto`）：Codex 未登录 / 用的是 API key / 登录已过期或将在 5 分钟内过期 /
  后端返回 401、403。被 401、403 拒绝后会先用 Excel 会话重试一次，之后一直用 Excel，直到 `auth.json`
  发生变化（例如你重新 `codex login`）才会再试 Codex。
- `EXCEL_BRIDGE_CODEX_AUTH` 可指向别的 `auth.json`（默认取 `$CODEX_HOME`，再默认 `~/.codex`）。
- 登录过期或不可用时，`excel-codex status` 会告诉你当前用的是哪一份、何时过期，并给出续期建议
  （Codex 那份重新 `codex login`；Excel 那份 `excel-codex login`）。
- 桥接从不刷新或写入任何 token；Codex 那份到期后请自行 `codex login`（`auto` 下会自动回退到 Excel）。
- [SUB2API 远端同步](docs/sub2api.md)也能送 Codex 的登录了：`push-session --login`（默认同 `auto`：先 Codex 再 Excel），
  所以没有 Excel 的机器（比如服务器）也能推。注意：这条命令会把**你选中的那份登录**发往你指定的服务器——
  这是你显式运行同步命令后才发生的凭据外发，只发给你自己信任的机器。

> **已验证**（2026-09-25）：用 Codex CLI 0.156.1 实测，重新 `codex login` 后 bps 后端返回 200，
> Codex 自己的登录可直接用、无需安装 Excel。万一日后后端变动拒绝它，`auto` 仍会自动回退到 Excel 会话。

此想法来自 [MIKUbiu/bps-local](https://github.com/MIKUbiu/bps-local)，本项目是它的一个独立实现，
详见[致谢与许可](#致谢与许可)。

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
excel-codex                                   交互式 Codex，默认 gpt-5.6-sol（Excel 链路）
excel-codex --model gpt-6-sol-1m-excel        换模型
excel-codex -- -c model_reasoning_effort=high 调推理强度
excel-codex -- exec "给 README 加一个目录"      非交互执行
excel-codex -- resume --last                  继续上次会话
excel-codex status                            看当前用哪份登录、是否可用、何时过期
excel-codex --login codex                      只用 Codex 自己的登录（不碰 Excel）
excel-codex login                             打开 Excel 的 ChatGPT 面板登录或续期
excel-codex desktop                           让 Codex 桌面版 / IDE 插件走 Excel 链路
excel-codex threads migrate                   把桥接自己名下的对话并进共享列表（启动时会自动做）
excel-codex threads migrate --from OpenAI     把中转站 provider（OpenAI）名下的对话并到 openai
```

启动器自己的选项：`--login <auto|codex|excel>`、`--model`、`--proxy`、`--timezone <auto|off>`、
`--image-model <模型名>`、`--port`、`--codex <路径>`、`--webview-dir <目录>`、`--skip-session-check`、
`--no-auto-signin`。

## 模型

每个模型都有两个版本，上游是同一个模型，只是上下文长度不同：

| 普通版（500k） | 1M 版 | 上游模型 |
| --- | --- | --- |
| `gpt-5.6-sol`（默认） | `gpt-5.6-sol-1m-excel` | `gpt-5.6-sol` |
| `gpt-5.6-terra` | `gpt-5.6-terra-1m-excel` | `gpt-5.6-terra` |
| `gpt-5.6-luna` | `gpt-5.6-luna-1m-excel` | `gpt-5.6-luna` |
| `gpt-6-sol` | `gpt-6-sol-1m-excel` | `gpt-6-sol` |
| `gpt-6-luna` | `gpt-6-luna-1m-excel` | `gpt-6-luna` |
| `gpt-6-astra` | `gpt-6-astra-1m-excel` | `gpt-6-astra` |

- 从 0.5.4 起，普通版在 Codex 里用和 OpenAI 官方一样的名字（模型列表里显示为「6-Sol Excel」这样），
  这样对话在开着桥接和关掉桥接时都能接着用，见[会话互通](#会话互通)。以前的 `gpt-6-sol-excel`
  这类名字照样能用，只是不再出现在模型列表里。1M 版官方没有，名字不变。

- **普通版**：上下文 500k，Codex 在 450k 时自动压缩（0.5.8 及更早是 272k、180k）。
- **1M 版**：上下文 918k，Codex 在约 826k 时自动压缩。918k 是真实后端的实测上限：一次最多接受约 918k
  输入 token，再多就返回 `context_length_exceeded`。gpt-5.6-sol、gpt-6-sol、gpt-6-luna、gpt-6-astra
  实测结果相同，gpt-5.6-terra 和 gpt-5.6-luna 按同一上限设置。
- 长对话每轮发出去的上下文更多，额度消耗也相应更多，用不到这么长时选普通版即可。
- 普通版和官方模型同名，官方链路按官方自己的上下文窗口处理。开着桥接时对话可以长到 450k 才压缩，
  关掉桥接后接着聊，超出官方窗口的部分 Codex 会先压缩；官方后端能不能接受这么长的压缩请求还没实测。
  对话很长又要关掉桥接时，建议开着桥接先压缩一次（`/compact`）。

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
   打开前会先查一次新版本，有就自动更新后再打开，见[自动更新](#自动更新windows-免安装版)。
2. **完全退出再打开 Codex 桌面版**（IDE 插件则重新加载窗口），模型列表里就是桥接的模型（显示名带 Excel）。
   Codex 登录过的话，以前的对话也在列表里，可以直接接着聊，见[会话互通](#会话互通)。
3. 用的时候保持这个窗口开着（可以最小化）。**关掉窗口或按 Ctrl+C，`config.toml` 按原样恢复。**
4. 在 Windows 上，这个窗口还会把系统时区对到 Codex 的出口时区，见[出口时区](#出口时区)。

细节：

- 改写前会把原文件存成 `config.toml.before-excel-codex`；你原来的 `model`、`model_provider`
  等行只是加注释停用，恢复时逐字节还原。
- `config.toml` 是所有 Codex 客户端共用的，窗口开着期间在终端直接运行 `codex` 也会走 Excel 链路。
- 想长期保持：`excel-codex desktop --keep-config`，之后用 `excel-codex desktop --off` 恢复
  （窗口意外被杀、配置没还原时也用它）。
- 换模型：`excel-codex desktop --model gpt-5.6-terra`；换端口：`--port`。
- 窗口开着期间会关掉 Codex 的 Apps 和插件推荐（`features.apps`、`features.remote_plugin`），
  随配置一起恢复。Codex 用 ChatGPT 登录时，打开对话要等 chatgpt.com 回应这两项（Apps 最多 30 秒，
  插件推荐每一轮 5 秒）；代理节点不通时，桌面版就一直停在“加载中”。想保留它们用 `--keep-apps`。

**桌面版打开对话一直在加载、新任务一直“启动中”**：多半是 Codex 自己连 chatgpt.com 连不上
（登录、Apps、插件都走它），这些请求不经过桥接。0.5.11 起桥接窗口开着时 Apps 和插件推荐已关掉，
最多还剩每条对话第一轮约 5 秒（Codex 读取账号设置）。仍然很慢的话，给 chatgpt.com 和
auth.openai.com 换一个稳定的代理节点；桥接窗口里出现 `could not reach chatgpt.com through the proxy`
时就是这个原因。

**报错 `The '…' model is not supported when using Codex with a ChatGPT account`**
（在 Codex 里退出登录后会变成 `401 Unauthorized: Missing bearer or basic authentication`，
地址是 `api.openai.com/v1/responses`）：这条对话的请求没有经过桥接，直接发给了 OpenAI 官方，
所以退出登录解决不了。官方没有的模型（1M 版，或 0.5.3 及更早版本里的 `*-excel` 名字）就会这样报错。
桌面版只在启动时读取 `config.toml` 和模型列表，常见原因：

- 桥接窗口已经关了（`config.toml` 已恢复），桌面版却没重启，列表里还留着桥接的模型；
- 关掉桥接后接着用一条 1M 版的对话：在模型菜单里换一个官方模型即可；
- Codex 没登录时（桥接是独立的 provider），这条对话是在开启桌面版模式之前建的；
- Cockpit Tools 这类工具把 Codex 切回了 ChatGPT 账号。

解决：打开 `excel-codex-desktop.cmd`，完全退出桌面版（文件 → 退出，或从托盘退出；只关窗口它可能
还在后台运行）再打开。发消息时桥接窗口里会出现 `"POST /v1/responses HTTP/1.1" 200`
这样的一行（0.4.2 起）；没有就说明请求还是没经过桥接。

**关掉桥接后接着聊，报 `stream disconnected before completion: 由于目标计算机积极拒绝，无法连接。
(os error 10061)`，并显示“正在重新连接 x/5”**：桥接窗口关了，Codex 却还在运行。开着桥接时打开的
对话会一直记着桥接的本机地址（`127.0.0.1:端口`），桥接关掉后那个端口没人监听，就被拒绝。只关窗口不够：
桌面版在 Windows 上关掉窗口后还在托盘里运行。要从托盘图标右键退出（macOS 用 `Cmd+Q`），IDE 插件要重新
加载窗口，再打开 Codex 就走官方链路了。桥接窗口关闭时会提示这一点；按 Ctrl+C 关闭时如果还看得到 Codex
进程，会多一句 `Codex is still running right now`。

**等了几分钟后报 `stream disconnected before completion: idle timeout waiting for SSE`，并显示“正在重新连接 x/5”**：
Codex 连续 5 分钟没收到任何数据，就当作断线重发请求，每次重发又要等 5 分钟。0.5.15 及更早版本在三种情况下会
让它空等：压缩长对话时（压缩请求不带工具，桥接不会告诉 Codex 还在进行）、模型写很长的工具调用时（调用写完
才转给 Codex）、后端接了请求却迟迟不开始回复时。0.5.16 起这些时候桥接也每 15 秒告诉 Codex 还在进行，升级即可。
升级后还出现，看看桥接窗口是不是被暂停了：在 Windows 的控制台窗口里点一下会进入选择文字状态（标题前出现
“选择”），这时桥接一写日志就会停住，按 Esc 或回车恢复。

**报 `stream disconnected before completion: stream closed before response.completed`，桥接窗口里是
`upstream stream ended abnormally: RemoteProtocolError`**：Excel 后端回复到一半断开了连接，有时断开前还用
Codex 不认的方式报了一个错。0.5.16 及更早版本只让 Codex 看到“流断了”，看不到原因。0.5.17 起桥接把原因转给
Codex：后端报了错就显示那个错，比如对话超出了模型的上下文窗口，Codex 会提示上下文已满，发下一条消息时先自动
压缩；后端什么也没说就断开，就显示是回复开始几秒后断开的、怎么断开的，Codex 照常自动重发。同一个对话每次都
这样断开，桥接窗口里 `the answer stopped after …` 那一行写着断开前收到了什么，反馈时请附上。

**升级后模型菜单还是旧的：只有 5.6-Sol、6-Astra、5.6-Terra、5.6-Luna，没有 6-Sol、6-Luna 和 1M 版**：
这是 0.5.1 及更早版本的模型列表。桌面版只在启动时读取模型列表，常见原因：

- 桌面版从那时起一直没有完全退出过（关掉窗口后它还在托盘里运行）：从托盘图标右键退出，再打开。
  0.5.15 起桥接窗口启动时如果看到 Codex 在运行，会提示 `Codex is running right now`；
- 打开的是旧文件夹里的 `excel-codex-desktop.cmd`（比如桌面快捷方式还指向旧版）：0.5.15 起桥接窗口里
  `now use the Excel bridge 0.5.15` 这一行写着正在运行的版本；
- 全手动用法（`serve` + `print-config`）：0.5.14 及更早版本的 `serve` 不更新模型列表文件，升级后
  重新运行一次 `print-config`；0.5.15 起 `serve` 启动时会自己更新。

也可以全手动：`excel-codex serve` 常驻桥接，再把 `excel-codex print-config` 输出的片段加进
`config.toml`，不用时删掉。`serve` 每次启动都会把片段指向的模型列表文件更新成当前版本的（0.5.15 起）。

## 会话互通

Codex 按对话建立时用的 provider 列出和继续对话。0.5.3 及更早版本的桥接是一个独立的 provider
（`excel-bridge`），所以开着桥接时看不到官方链路的旧对话，关掉桥接后桥接建的对话也接不上。

从 0.5.4 起，**只要 Codex 自己登录过**（`codex login`，ChatGPT 账号或 API key 都行），桌面版模式和
`excel-codex` 启动的 Codex 都改为把 Codex 自带的 `openai` provider 指到桥接（`openai_base_url`），
模型也用官方的名字。于是：

- 开着桥接：官方链路建的旧对话都在列表里，接着聊就走 Excel 链路。
- 关掉桥接：开着桥接时建的对话照样在列表里，接着聊就走官方链路。1M 版的对话需要先在模型菜单里
  换成官方有的模型。
- 两个后端各自加密的推理内容（reasoning）不一定互认。Excel 后端不认官方那份时，桥接会去掉这些
  推理内容再发一次，对话文字不受影响（桥接窗口里会有一行说明）。反过来官方后端是否认 Excel 后端
  那份还没有实测；如果关掉桥接后某条对话报错，新建一条对话即可。
- 用 `codex exec` 时会看到一行 `failed to connect to websocket: 426`：Codex 自带的 provider 先试
  WebSocket，桥接只讲 HTTP，回 426 让它马上改用 HTTP，属于正常现象。

Codex 没登录时，自带的 provider 会先要求登录，所以桥接仍是独立的 `excel-bridge` provider，行为和
以前一样：开着桥接建的对话只在开着桥接时出现在列表里。之后 `codex login`，这些对话会按下一节自动并进
共享列表。

### 桥接自己名下的对话

0.5.3 及更早版本、或 Codex 没登录时经桥接建的对话，记在 `excel-bridge` 名下。它们不在共享列表里，
关掉桥接后 Codex 也打不开，会报“Model provider `excel-bridge` not found”。Codex 登录过（`codex login`）
时，桥接会自动把它们并进共享列表：

- `excel-codex-desktop.cmd`（`excel-codex desktop`）和 `excel-codex` 启动时，如果 Codex 已经完全退出，
  就自动迁移，窗口里会显示 `Moved N conversation(s) … into the shared list`。
- Codex 还开着（桌面版、IDE 插件，或终端里的 `codex`）时不迁移，只提示一句：Codex 运行时会把改动
  改回去，也可能正在写这些对话文件。所以要**先开桥接、再开桌面版**；或者退出 Codex 后运行
  `excel-codex threads migrate`。
- 迁移后这些对话记到 `openai` 名下，模型名换成官方的（`gpt-6-sol-excel` → `gpt-6-sol`）。官方没有
  1M 版，1M 版的对话换成同一模型的官方版（`gpt-6-sol-1m-excel` → `gpt-6-sol`）；开着桥接时想继续
  用 1M，在模型菜单里再选回来。关掉桥接也能打开、接着聊（走官方账号或中转站都行），改名、归档也不会再
  变回去。

改动内容：

- Codex 的对话索引（`~/.codex/state_<n>.sqlite` 里的 threads 表）。改之前先复制一份，存为同目录下的
  `state_<n>.sqlite.before-excel-codex-<时间>`。
- 每个对话文件（`~/.codex/sessions` 下）里 Codex 重建索引要读的几处：第一行的 provider，以及后面
  `turn_context`、`thread_settings_applied` 行里的模型名和 provider。Codex 会按这些重建索引，只改索引
  的话，它会把对话改回 `excel-bridge` 或桥接的模型名。每处只原地换掉这一个值，文件其余内容、对话内容
  和修改时间都不变。
- 旧版本迁移过的对话会补齐：0.5.4、0.5.5 只改了索引；0.5.6 到 0.5.8 没改后面几行的模型名，1M 版也
  没换，关掉桥接后 Codex 可能又用回桥接的模型名，走官方或中转站都会失败。下次在 Codex 完全退出时启动
  桥接，窗口里会显示 `Finished N conversation(s) moved by an earlier excel-codex`。
- `excel-codex threads` 列出还在 `excel-bridge` 名下的对话。`excel-codex threads undo`（同样要先退出
  Codex）撤销：迁移改过、之后没被 Codex 改过的对话放回 `excel-bridge` 名下。
- 不想自动迁移：设置 `EXCEL_BRIDGE_AUTO_MIGRATE=0`，需要时自己运行 `excel-codex threads migrate`。

### 中转站名下的对话

报“Model provider `OpenAI` not found”（或别的名字）的，多半是经中转站建的对话。有的中转站生成的
Codex 配置（例如 SUB2API 的“使用密钥 → Codex”）用的是它自己的 provider，名字叫 `OpenAI`：
`model_provider = "OpenAI"` 加一段 `[model_providers.OpenAI]`。它和 Codex 自带的 `openai` 不是同一个，
**provider 名字区分大小写**。经它建的对话记在 `OpenAI` 名下；`config.toml` 里一旦没有这段（换回官方
账号、切换账号的工具改写了配置，或删掉了中转站配置），Codex 就打不开它们。这和桥接无关，但 0.5.10 起
桥接可以把它们并到 `openai` 名下：

```bash
excel-codex threads                          # 末尾会列出其他 provider 名下各有几个对话
excel-codex threads --from OpenAI            # 列出 OpenAI 名下的对话
excel-codex threads migrate --from OpenAI    # 并到 openai 名下（先完全退出 Codex）
```

- 改法和上一节一样：先复制对话索引，对话文件里只原地换掉 provider（和桥接的模型名）这几处。
  `excel-codex threads undo` 把它们放回 `OpenAI` 名下。
- 名字要和 Codex 记的一字不差；`--from` 找不到时会列出 Codex 里实际有的 provider 名字。
- 并过去之后，走官方账号、开着桥接都能接着聊。以后经 `OpenAI` 新建的对话还会记在 `OpenAI` 名下。
- 想让中转站的对话也一直在同一个列表里，就把中转站配成 Codex 自带的 provider：删掉
  `model_provider = "OpenAI"` 和 `[model_providers.OpenAI]`，改成一行
  `openai_base_url = "https://你的中转站/v1"`，再用中转站的 key 登录 Codex
  （`codex login --with-api-key`，从标准输入读入 key）。**`openai_base_url` 指向中转站时不要用 ChatGPT
  账号登录**，否则 Codex 会把 ChatGPT 的登录凭据发给中转站。

## 出口时区

Codex 会把本机的时区和日期写进每个对话（`<environment_context>` 里的 `<timezone>`、`<current_date>`）。
请求从代理出口发出，本机时区和出口所在地对不上时，模型会以为你在另一个时区。

- **经过桥接的请求**：桥接先查出口 IP（`https://bps.openai.com/cdn-cgi/trace`），再查这个 IP 的时区
  （依次问 ipwho.is、ipapi.co、get.geojs.io、api.ip.sb），把请求里的时区和当天日期换成出口那边的。
  每 5 分钟看一次出口 IP 有没有变。
- **官方 ChatGPT 登录**（Windows）：这条链路不经过桥接，而 Codex 读的是 Windows 的系统时区。
  `excel-codex-desktop.cmd` 打开期间会按 Codex 选代理的方式（`HTTPS_PROXY` / `ALL_PROXY`，
  否则用 Windows 的代理设置，不支持 PAC 脚本）查 `chatgpt.com`（API key 登录则是 `api.openai.com`）的出口时区，启动时和之后每分钟
  用 `tzutil` 把系统时区对上，关掉窗口（右上角关闭或 Ctrl+C）时恢复成原来的时区。不需要另外运行
  别的程序或计划任务。
- **以 Cloudflare 看到的国家为准**：查出口 IP 时 Cloudflare 也会给出它认为出口在哪个国家（OpenAI 看到的
  就是这个）。定位服务给出的国家和它不一致时不采用、换下一家；都不一致就不改时区，并说明各家的结果。

注意：

- 改的是整台电脑的时区。窗口被强制结束（例如任务管理器）时来不及恢复，运行
  `excel-codex timezone restore` 恢复第一次修改前的时区。
- 只把出口 IP 发给这几个定位服务查时区，走的也是同一个代理，不发送别的信息。同一个 IP
  的结果会缓存。
- `--timezone off`（或环境变量 `EXCEL_BRIDGE_TIMEZONE=off`）关掉以上两项。
- 代理在几个节点之间轮换（比如台湾、日本来回切）时，系统时区不跟着来回跳：新的出口时区要连续
  3 次检查都一样才采用（每分钟查一次，约 2 分钟），等待期间窗口里说一次。桥接也记得查过的出口 IP 的时区（12 小时），
  来回切时不用重新查。
- Windows 的“自动设置时区”开着时，系统会把时区改回本地的。窗口会再改回去，第一次时说明原因；
  可以在 设置 > 时间和语言 > 日期和时间 里关掉“自动设置时区”。
- 除了启动时的第一次，偶尔一次查询失败不会在窗口里报错，同一种错误连续出现两次才说，恢复时说一声。代理节点连不上时报
  `could not reach … through the proxy`（代理地址里的密码不显示）。
- 窗口里日志的时间一直按窗口启动时的时区显示。
- `excel-codex timezone` 查看出口时区和当前状态；`excel-codex timezone sync --probe` 只查不改。
- 显示 `(Cloudflare: TW)` 这类和预期不同的国家，说明代理把 OpenAI 的流量分流到了那个国家的节点，
  OpenAI 看到的就是那里；代理软件首页显示的只是默认节点。通过同一个代理打开
  `https://chatgpt.com/cdn-cgi/trace`，`loc=` 那一行就是 OpenAI 看到的国家。

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

## 限流

用加载项的所有人按模型共用一份每分钟 token 额度（TPM）。额度用满时，后端立刻回一个
`rate limit exceeded`，说几毫秒后再试；Codex 就按这个间隔重试 5 次，不到一秒就用完，这一轮报错
（界面上显示“Reconnecting 5/5”）。

所以桥接会先自己等：回答还没开始就被限流时，隔 1、2、4、8、15 秒（之后每次 15 秒）把同一个请求再发一次，
最多等 5 分钟。这期间 Codex 只显示在工作（桥接每 10 秒告诉它还在进行，不会触发它 5 分钟无响应就断开），
桥接窗口（CLI 模式下是 `bridge.log`）里会写 `the Excel backend is rate limited … trying again in N s`。
等满 5 分钟还被限流，Codex 显示 `The Excel backend is still rate limited after 5 minutes …`，这一轮结束，
稍后再发一次消息即可；Codex 不会再自己重试（否则每次重试都要再等 5 分钟）。中途随时可以在 Codex 里中断。

- 回答开始后才失败的不重发，免得内容重复；别的错误照原样交给 Codex。
- `EXCEL_BRIDGE_RATE_LIMIT_WAIT=<秒>` 改最多等多久：默认 `300`，最多 `1800`；`0` 不等，
  报错照原样交给 Codex（它会按自己的方式很快重试 5 次）。

## 网络中断

代理节点掉线或网络闪断时，请求发不到 `bps.openai.com`，Codex 会在几秒内重试 5 次后报
`502 Bad Gateway: Could not connect to bps.openai.com (ConnectError)`。0.5.11 起桥接自己接着连：

- 后端还没回应的请求（连不上、TLS 握手被断等），隔 0.5、1、2、4、8、15 秒（之后每次 15 秒）把同一个
  请求再发一次，最多等 2 分钟。前 5 秒 Codex 什么都看不到；之后它只显示在工作，桥接每 10 秒告诉它还在进行。
  连上了就照常回答。
- 等满 2 分钟还连不上，Codex 显示 `Still no connection after 2 minutes …`，这一轮结束，网络恢复后再发一次
  即可；Codex 不会再自己重试。中途随时可以在 Codex 里中断。
- 后端已经回应之后才断的不重发（比如读回答时超时），免得内容重复。
- `EXCEL_BRIDGE_CONNECT_WAIT=<秒>` 改最多等多久：默认 `120`，最多 `1800`；`0` 不重试，
  报错照原样交给 Codex。

## 子代理

Codex 的子代理（多代理 v2 的 `collaboration.spawn_agent` / `send_message` / `followup_task`）可以用。
0.5.11 及更早会出这两种问题，老会话更常见：

- **子代理报“加密内容无法解码”**：桥接转交的调用没有注明参数是明文，Codex 就把发给子代理的任务标成
  “加密内容”，Excel 后端解不开。这些消息留在对话历史里，所以老会话反复出错，新会话没用过子代理就没有。
- **工具调用越来越容易失败**：桥接记不起原始调用时（重启后，或 SUB2API 多人共用、缓存被挤掉时）会重建它，
  这时丢了 `collaboration.` 前缀，或者把模型没转换成功的 `run_officejs` 调用又包一层。模型照着历史学，
  就越错越多，而且只收到一句笼统的“格式不对”。

0.5.12 起：

- 桥接转交的每个调用都注明参数是明文，子代理收到的是普通文字。
- 老会话里被误标成加密的消息，在发给后端前还原成文字，不用新开会话。
- 重建的调用保留完整的工具名；没转换成功的调用原样回放、不再多包一层，并告诉模型具体错在哪里
  （比如“`spawn_agent` 不在工具目录里，目录里叫 `collaboration.spawn_agent`”）。

真正由别的后端加密的内容（比如关掉桥接、用官方登录接着聊时留下的）Excel 后端读不了。后端因此报错时，
桥接把这些内容换成一句说明再发一次，窗口里写 `the Excel backend could not read what another backend encrypted`。

## 不带本工具模型目录的 Codex（中转站配置、Cockpit）

用中转站的 Codex 配置模板、Cockpit Tools 或自己写的 provider 连桥接（包括 SUB2API）时，Codex 没有本工具
的模型目录，对 gpt-5.6 / gpt-6 用它自带的设置：工具不放在请求的 `tools` 里，而是放进一条 `additional_tools`
输入（Responses Lite），并且只给模型 code mode 的 `exec`（在里面写 JavaScript 调用 shell 等工具）和 `wait`。
0.5.13 及更早的桥接读不到这些工具，模型的每次调用都报 `exec is not a tool in the catalog`。

0.5.14 起桥接从这条输入里读出工具和指令，模型通过 `exec` 调用工具，和官方登录时一样。模型直接调用
`exec_command` 这类嵌在 `exec` 里的工具时，会被告知改用 `exec`；反过来，在官方 code mode 里开始的对话换到
本工具的模型目录后，模型照着历史去调用 `exec`，也会被告知直接调用目录里的工具。

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

**桌面版提示「此模型不支持图像输入」**：这是桌面版自己拦下的，请求没有发出。它按模型目录判断
能不能贴图，而且只在启动时读取目录：

- 0.3.1 及更早版本写的目录把 Excel 模型标成只收文字。升级后重新运行桌面版模式
  （`excel-codex-desktop.cmd` 或 `excel-codex desktop`），再完全退出桌面版（托盘里退出，
  macOS 按 `Cmd+Q`）重新打开。
- 用 Cockpit Tools 这类工具管理 Codex 时，模型目录是它生成的，不是本工具。在它的模型供应商设置
  里把 `*-excel` 模型的「图片输入」打开（Cockpit 1.3.57 及更早默认关闭）。

## 生图

从 0.4.6 起可以用 Codex 自带的生图工具：让 Codex「画一张……」或「把这张图改成……」即可。
生成的图显示在对话里，同时存到 `~/.codex/generated_images/`。

- Codex 只在 provider 设置了 `x-openai-actor-authorization` 请求头时才提供这个工具。启动器的
  `-c` 参数、桌面版模式和 `print-config` 都会加上（值是 `excel-codex-bridge`，桥接不使用这个值，
  也不往外发）。以前自己手动加过这个头的，升级后可以删掉。
- 桥接把请求转给加载项生图用的接口：新图发到 `bps.openai.com/basispoints/api/images/generations`，
  改图发到 `/images/edits`，用同一个登录会话，参数也和加载项一致：模型 gpt-image-2，PNG，
  尺寸 `auto`、`1024x1024`、`1536x1024`、`1024x1536`、`1280x720`。
- 不支持透明背景（加载项本身也不支持）。模型要透明背景时会收到说明，可以改用普通背景再画。
- 生图额度按你的 ChatGPT 套餐算。后端拒绝时，Codex 里会显示后端给的原因。
- **指定生图模型**：Codex 的生图工具固定请求 gpt-image-2，也不会把实际用的模型告诉对话里的模型，所以
  在对话里问「用的是不是某某模型」得不到确切答案。想用别的模型（比如 `gpt-image-2.5`），启动时加
  `--image-model gpt-image-2.5`（或设环境变量 `EXCEL_BRIDGE_IMAGE_MODEL`），桥接会改为向后端请求这个模型。
  桥接窗口启动时显示当前的生图模型，每次生图还会记一行 `image generations with gpt-image-2.5: 1 picture(s) came back`，
  以这里为准。后端是否接受这个模型名由后端决定，不接受时 Codex 里会显示后端给的原因。
- 用 Codex 自己的 ChatGPT 登录共享会话时（见[会话互通](#会话互通)），Codex 对自带 provider 本来就提供
  这个工具；用 API key 登录时 Codex 不提供。
- 用 Cockpit Tools 这类工具管理 Codex 时，provider 是它写的，需要在它的供应商配置里自己加上
  `http_headers = { "x-openai-actor-authorization" = "excel-codex-bridge" }`。

## 检查更新

从 0.4.4 起，程序会在后台查 GitHub 上有没有新版本（最多每 12 小时一次），有就显示版本号、更新内容和下载链接：

- 桌面版模式和 `serve` 的窗口：启动后显示；窗口一直开着时，新版本发布后也会显示。
- 用 `excel-codex` 启动 Codex 时：Codex 退出后显示。双击打开的窗口会停住，按回车再关。

这个请求只发给 `api.github.com`，除了 User-Agent 里的版本号不带任何信息，代理设置和访问上游时相同。
查不到就不提示，不影响使用。设置 `EXCEL_BRIDGE_UPDATE_CHECK=0` 可以关掉（自动更新也一起关掉）。

手动更新时先关掉正在运行的桥接窗口和 Codex，再把新版解压到原目录覆盖（或换个目录）。设置和状态不在安装目录里，
不会丢。从源码运行的 `git pull` 即可。

### 自动更新（Windows 免安装版）

从 0.5.13 起，双击 `excel-codex-desktop.cmd` 会先查一次新版本，有就先更新、再用新版打开：

1. 下载新版的 Windows 压缩包，用 GitHub API 列出的 SHA-256 校验；
2. 解压到安装目录下的 `.update` 文件夹，先运行一次新版的 `excel-codex.exe`，确认它能启动；
3. 当前程序退出后，把 `excel-codex.exe`、`_internal` 和 `excel-codex-desktop.cmd` 换成新版，再打开新版。

窗口里会显示下载进度，按 Esc 可以这次先跳过，直接打开当前版本。任何一步不成功（下载失败、校验不对、
新版起不来、文件被占用），都会照常打开当前版本，失败的更新一小时后再试。同一目录的 excel-codex
还在别的窗口里运行时不会替换，关掉它再双击即可，已经下载的不用重下。

- 0.5.12 及更早的版本没有这个功能，需要手动更新一次。
- 只有 `excel-codex-desktop.cmd` 会自动更新。直接双击 `excel-codex.exe`、macOS 版和源码运行仍然只提示。
  在免安装版里运行 `excel-codex update` 可以提前下载好，下次双击 `excel-codex-desktop.cmd` 时装上。
- 目录名里的版本号不会跟着变，以启动时的提示或 `excel-codex --version` 为准。
- 设置 `EXCEL_BRIDGE_AUTO_UPDATE=0` 只提示、不自动更新。
- GitHub 下载慢时，可以把 `EXCEL_BRIDGE_DOWNLOAD_MIRROR` 设为镜像前缀（形如 `https://<镜像>/`，
  会拼在 GitHub 下载链接前面）。下载的文件仍按 GitHub API 列出的 SHA-256 校验，镜像改动过就不会装。
- 发布包没有代码签名：校验能挡住下载损坏和被改动的镜像，挡不住 GitHub 仓库本身被攻破。

## 限制

- 只实现 Responses API，没有 `/responses/compact` 端点。
- 单个请求解压后最大 1 GiB。0.5.10 及更早是 64 MiB：长对话（尤其带图片）在 450k 自动压缩前就会超过，
  报 `Invalid request body: request body is too large`。
- Excel 后端会给每个请求加上约 2.2 万 token 的固定前缀，大部分命中缓存；额度按你的 ChatGPT 套餐计算，
  同时开的会话越多用得越快。
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
| `EXCEL_BRIDGE_LOGIN` | 用哪份登录：`auto`（默认）/ `codex` / `excel`（同 `--login`），见[登录方式](#登录方式) |
| `EXCEL_BRIDGE_CODEX_AUTH` | Codex 的 `auth.json` 路径，默认 `$CODEX_HOME/auth.json` 再默认 `~/.codex/auth.json` |
| `EXCEL_BRIDGE_PROXY` | 出站代理（同 `--proxy`） |
| `EXCEL_BRIDGE_HOME` | 状态目录：模型目录 JSON、`bridge.log`、登录用工作簿，以及让重启后仍能原样回放历史工具调用的 `tool-calls.sqlite3`（保留 60 天）。默认 `%LOCALAPPDATA%\excel-codex-bridge` 或 `~/.excel-codex-bridge` |
| `EXCEL_BRIDGE_AUTO_SIGNIN` | 设为 `0` 时不自动打开 Excel（同 `--no-auto-signin`） |
| `EXCEL_BRIDGE_UPDATE_CHECK` | 设为 `0` 时不检查更新，也不自动更新 |
| `EXCEL_BRIDGE_AUTO_UPDATE` | 设为 `0` 时 `excel-codex-desktop.cmd` 只提示新版本、不自动更新，见[自动更新](#自动更新windows-免安装版) |
| `EXCEL_BRIDGE_DOWNLOAD_MIRROR` | 自动更新下载时拼在 GitHub 下载链接前面的镜像前缀，仍按 GitHub 列出的 SHA-256 校验 |
| `EXCEL_BRIDGE_AUTO_MIGRATE` | 设为 `0` 时启动不自动迁移桥接自己名下的对话，见[桥接自己名下的对话](#桥接自己名下的对话) |
| `EXCEL_BRIDGE_TIMEZONE` | `auto`（默认）/ `off`，同 `--timezone`，见[出口时区](#出口时区) |
| `EXCEL_BRIDGE_IMAGE_MODEL` | 生图工具向后端请求的模型，默认 `gpt-image-2`（同 `--image-model`），见[生图](#生图) |
| `EXCEL_BRIDGE_RATE_LIMIT_WAIT` | 被限流时最多等多少秒再把报错交给 Codex，默认 `300`（5 分钟），`0` 不等，最多 `1800`，见[限流](#限流) |
| `EXCEL_BRIDGE_CONNECT_WAIT` | 连不上后端时最多再试多少秒，默认 `120`（2 分钟），`0` 不重试，最多 `1800`，见[网络中断](#网络中断) |
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

**用 Codex 自己的登录直连 Excel 后端（从而无需安装 Excel）这一想法**，来自
[MIKUbiu/bps-local](https://github.com/MIKUbiu/bps-local)（Unlicense）。感谢 MIKUbiu 的思路。
本项目是它的一个独立实现：把这条登录作为可选来源接进原有的桥接与回退机制，实现代码为本项目自写。
bps-local 的 Basispoints 协议部分源自 [hloolx/codex2api](https://github.com/hloolx/codex2api)（MIT，
经 [ranxi2001/sub2api](https://github.com/ranxi2001/sub2api) 移植）；本项目对应部分则来自上文的 ghcp_proxy。

感谢 [LINUX DO](https://linux.do) 社区。

## 交流群

QQ 群「Vibe coding 交流」，群号 **966195257**，扫码加入：

<img src="docs/qq-group.jpg" alt="QQ 群 966195257 二维码" width="240">

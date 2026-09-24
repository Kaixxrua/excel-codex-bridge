# excel-codex-bridge

**简体中文** | [English](README.en.md)

> **非官方项目**，与 OpenAI、Microsoft 无任何关联，也未获其认可。使用前请先读[风险与免责声明](#风险与免责声明)。

用你**自己电脑上**已登录的 ChatGPT Excel 加载项会话来跑 [Codex CLI](https://github.com/openai/codex)：
双击或一条命令，本地桥接和 Codex 一起启动，退出 Codex 时桥接自动关闭。
不代你登录，不监听本机以外的地址，会话只在内存里，只发往 OpenAI。

## 工作原理

```
Codex CLI ──(Responses API, 127.0.0.1)──▶ excel-codex-bridge ──(HTTPS)──▶ bps.openai.com
                                                 ▲                      （ChatGPT for Excel 后端）
                              只读 Excel 加载项本地缓存的登录态（不落盘）
```

- **不做任何登录**：不实现 OAuth，不碰账号密码。登录只在 Excel 的 ChatGPT 面板里完成，
  本工具只读取加载项在本机 WebView2 存储里缓存的那份会话（`bps_auth_tokens`）。
- **token 只在内存里**：不写盘、不上传，只随请求发往 `bps.openai.com`；日志只有请求行和错误类型，
  不含提示词和 token。
- **只给本机用**：只监听回环地址；拒绝非回环来源、非回环 `Host`（防 DNS rebinding）
  以及任何带 `Origin` 头的浏览器请求。`serve` 拒绝绑定 `0.0.0.0`。
- **工具调用**：Excel 后端不接受客户端自带工具。桥接把 Codex 的工具（shell、apply_patch…）
  写进提示词，模型通过后端原生的 `run_officejs` 回传调用，桥接再还原成 Codex 的工具调用。
- **不改你的 Codex 配置**：启动器用 `-c` 参数临时指定 provider 和模型目录，
  `~/.codex/config.toml` 保持原样，平时直接运行 `codex` 不受影响。

## 前提

- Windows 10/11 + **Microsoft 365 桌面版 Excel**，已安装加载项 **ChatGPT**（发布者 OpenAI）
  并在面板里登录过一次；你的 ChatGPT 套餐需要能用这个加载项。
- Python 3.10+（[python.org](https://www.python.org/downloads/)，安装时勾选 *Add python.exe to PATH*）。
- Codex CLI：`npm install -g @openai/codex`。
- 能访问 `bps.openai.com`（需要代理见[代理](#代理)）。

## 快速开始（Windows）

1. 把本仓库下载或 clone 到任意目录，例如 `D:\tools\excel-codex-bridge`。
2. 打开 Excel → 开始 → 加载项 → **ChatGPT**，确认面板里已登录。之后 Excel 可以关掉。
3. 在你的项目目录打开 cmd 或 PowerShell，运行：

   ```
   D:\tools\excel-codex-bridge\excel-codex.cmd
   ```

   首次运行会在仓库目录建 `.venv` 并安装依赖，之后秒开。也可以直接**双击** `excel-codex.cmd`，
   这时 Codex 在你的用户目录启动。把仓库目录加进 `PATH` 后，任意目录输入 `excel-codex` 即可。

常用写法（`--` 之后的参数原样交给 Codex）：

```
excel-codex                                   交互式 Codex，默认 gpt-5.6-sol-excel
excel-codex --model gpt-5.6-terra-excel       换模型
excel-codex -- -c model_reasoning_effort=high 调推理强度
excel-codex -- exec "给 README 加一个目录"      非交互执行
excel-codex -- resume --last                  继续上次会话
excel-codex status                            检查会话是否可用、何时过期
```

启动器自己的选项：`--model`、`--proxy`、`--port`、`--codex <路径>`、`--webview-dir <目录>`、
`--skip-session-check`。

## 模型

| Codex 里选择 | 上游模型 | 上下文 |
| --- | --- | --- |
| `gpt-5.6-sol-excel`（默认） | `gpt-5.6-sol` | 272k |
| `gpt-5.6-terra-excel` | `gpt-5.6-terra` | 272k |
| `gpt-5.6-luna-excel` | `gpt-5.6-luna` | 200k |
| `gpt-6-astra-excel`（实验性） | `gpt-6-astra` | 按 272k 处理 |

推理强度 `low` / `medium` / `high` / `xhigh`，默认 `medium`。

`gpt-6-astra-excel` 还没确认 Excel 后端支持。如果报模型不可用，说明你的账号或 Excel 后端还没开放它。
遇到新模型时，也可以设置 `GHCP_EXCEL_UPSTREAM_MODEL=<上游模型名>`，把所有请求强制发给那个模型来试。

## 代理

桥接访问 `bps.openai.com` 的出站：

- `--proxy http://127.0.0.1:7890`（也支持 `socks5h://…`），或环境变量 `EXCEL_BRIDGE_PROXY`；
- 都没设时跟随 `HTTPS_PROXY` 和系统代理。

TLS 证书校验始终开启。Codex 到桥接走 `127.0.0.1`，启动器会自动把它加进 `NO_PROXY`。

## 在 Codex IDE 插件 / 桌面版中使用

这些客户端没法传 `-c`，改成常驻桥接加配置文件：

```
excel-codex serve          保持窗口开着，监听 127.0.0.1:8765
excel-codex print-config   输出要加进 ~/.codex/config.toml 的片段
```

不用时把那几行从 `config.toml` 删掉即可恢复。

## macOS / WSL（实验性）

- **macOS**：`./excel-codex.sh`，读取 Excel 沙盒里的 WebKit 本地存储。
- **WSL**：Excel 装在 Windows 侧，把它的数据目录指给桥接：

  ```
  ./excel-codex.sh --webview-dir /mnt/c/Users/<你>/AppData/Local/Microsoft/Office
  ```

## 会话过期

加载项的 token 大约 10 天有效。桥接每次请求前都会检查，过期或临近过期时重新读取本机缓存，
所以只要在 Excel 里打开一次 ChatGPT 面板让它刷新，**不用重启**桥接或 Codex。
`excel-codex status` 可查看剩余时间。

## 限制

- 只支持文本输入，不能贴图片。
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
| `EXCEL_BRIDGE_HOME` | 状态目录：模型目录 JSON 和 `bridge.log`。默认 `%LOCALAPPDATA%\excel-codex-bridge` 或 `~/.excel-codex-bridge` |
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

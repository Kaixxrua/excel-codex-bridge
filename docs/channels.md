# Codex Channels：多通道对照研究

项目保留 `excel-codex-bridge` 仓库、包名和旧启动器，新增独立研究入口。日常桥接和研究分开运行；
双击 `codex-channels.cmd`（Windows）或 `codex-channels.command`（macOS）只列出通道，不发推理请求。
Linux 使用 `./codex-channels.sh`。安装 Python 包后也可直接运行 `codex-channels`。
下文统一用 `excel-codex research`，等价于这些入口；Windows PowerShell 使用 `./excel-codex.exe research`。

## 通道与凭据

| 研究 kind | 凭据 / 目的地 | 研究范围 | 日常桥接 |
|---|---|---|---|
| `codex-http` | 本机 Codex ChatGPT 登录；固定 Codex HTTPS 端点 | 文本、原生工具、完整历史续轮 | `--route codex`，默认 |
| `codex-ws` | 同一份 Codex 登录；固定 Codex WSS 端点 | 相同题目及工具，通过 WebSocket 上游执行 | `--route codex-ws` |
| `codex-cli` | 同一账号的隔离临时登录；官方 Codex CLI | 文本基线；CLI 的系统提示和内部调用不能视为与原始 API 等价 | 直接运行 Codex |
| `siwc` | 单独经过签名验证并获 direct 权限的 SIWC 授权；`api.openai.com` | Responses 协议、目录、题目及工具 | 研究入口 |
| `responses-http` | 显式服务器 URL 和独立 API key 引用 | OpenAI Responses 兼容服务，包括自有 SUB2API | 研究入口 |
| `bps` | 指定 Codex 登录；固定 BPS 端点 | 单次严格文本调用；不使用旧桥接的自动重试与合成完成 | `--route excel` 保留旧适配 |

WebSocket 日常桥接的客户端仍使用 HTTP Responses，桥接到上游的连接为 WebSocket。每次请求发送完整历史，
不依赖跨请求缓存或 `previous_response_id`，也不会在 WS 失败时切回 HTTP。普通桥接不执行实验工具。
研究中的工具只有受限的合成记录读取，SQL 只在本地内存 fixture 上只读执行；不执行模型生成的 shell 或代码。

## 一次完整研究

```sh
codex login
excel-codex research routes
excel-codex research prepare --out studies/smoke-01 --routes codex-http,codex-ws,codex-cli --profile smoke --model gpt-5.6-sol --effort low
excel-codex research request-diff --study studies/smoke-01
excel-codex research inventory --study studies/smoke-01
excel-codex research run --study studies/smoke-01
excel-codex research report --study studies/smoke-01
```

`prepare` 离线生成合成题、答案、固定随机顺序和调用预算；不覆盖已有目录。默认生成新种子，`--seed` 可复现。
题目覆盖记录转换、依赖排程、只读 SQL 和工具续轮；smoke 只包含记录转换与工具续轮。
`request-diff` 离线展示第一题的公共请求、实际适配体和哈希。CLI 额外提示以及第三方网关内部改写不能由此证明相同。
`inventory` 只读取所选凭据，不发推理；`run` 是明确开始消费额度的命令，并在推理前查询原生/SIWC 账号的模型目录。
请求中的模型、推理档位、工具和历史不会被悄悄删减或降级。认证失败不会改用另一份账号或计费方式。

研究目录包含 `manifest.json`、`ledger.sqlite3`、`report.json` 和 `report.md`。题目和配置哈希同时保存在账本中；
修改配置、替换题目或删除账本都不能重置预算后继续同一研究。账号绑定不随 access token 正常更新而改变。
换账号、授权注册或 API key 后，请创建新研究。

### 预算、中断与续跑

默认预算按所有路线、题目和最大续轮数计算；可以在 `prepare` 时用 `--max-calls` 设更低值。
smoke / screen / confirm 的硬上限分别为 32 / 192 / 512 次客户端提交；单次等待默认 120 秒，最高 600 秒。
每次提交先写入 SQLite 并落盘，再发网络请求或启动 CLI。失败、取消和完成未知都计入预算。

```sh
excel-codex research status --study studies/smoke-01
# 同一命令续跑，跳过所有已提交或已完成的实验，不重复已消费额度。
excel-codex research run --study studies/smoke-01
```

进程锁阻止两个运行者同时使用同一研究；进程退出后系统释放锁，无需手工删锁。
若中断丢失了内存里的工具续轮状态，该实验标记为中断，不重新提交。协议错误会停止本研究中该通道后续实验；
答题错误则保留评分，并继续下一题。修复协议后要准备新研究，旧失败证据保留。
客户端超时不能保证上游已停止生成。CLI 的一次启动和中转服务的一次提交可能包含内部重试，报告不会把它们冒充精确上游推理次数。
本版本限制客户端提交与等待时间，不承诺服务端 token 硬上限。

### 筛选与跨时间复验

```sh
excel-codex research prepare --out studies/screen-01 --routes codex-http,codex-ws --profile screen
excel-codex research run --study studies/screen-01
excel-codex research prepare --out studies/confirm-01 --routes codex-http,codex-ws --profile confirm --window-gap 3600
excel-codex research run --study studies/confirm-01 --window 1
# 至少一小时后执行第二窗口；不会自动等待或发后台请求。
excel-codex research run --study studies/confirm-01 --window 2
```

新的 confirm 默认使用新的随机题集；不要为了追逐高分重复挑选种子或覆盖失败数据。
报告分别展示已提交/已完成、已评分/通过、端到端耗时、未执行、未知完成和配对胜负。
只有同一所选 Codex 账号的 HTTP/WS、且实际返回模型和 effort 一致的完成任务，才进入严格原生配对。
其他比较明确标为探索性：API key 不证明后端账号，SIWC `sub` 不证明工作区，CLI 包含不同的执行环境。
协议失败不计成“答错题”；无法配对会单列。相关题目变体、天花板效应和少量样本不支持“满血恢复”或一般质量优势结论。

## 配置自有 Responses 服务

配置文件只放引用。`auth_file`、`key_file`、`executable` 必须是绝对路径；不能嵌入 token、带凭据的 URL 或任意上游请求头。
所有 HTTP 路线禁重定向。自定义端点必须 HTTPS；HTTP 仅允许明确的本机回环地址。

```json
{
  "profile": "smoke",
  "seed": 20261008,
  "model": "gpt-5.6-sol",
  "effort": "low",
  "max_calls": 9,
  "routes": [
    {"id": "direct", "kind": "codex-http"},
    {"id": "socket", "kind": "codex-ws"},
    {"id": "my-sub2api", "kind": "responses-http", "base_url": "http://127.0.0.1:18080/v1", "key_env": "MY_SUB2API_KEY"}
  ]
}
```

自行设置 `MY_SUB2API_KEY`，然后 `excel-codex research prepare --config channels.json --out studies/custom-01`。
它是目标服务自己的 API key；原生 Codex 凭据不会被发送给自定义端点。也可用 `key_file` 引用本机秘密文件，二者只能选一项。
这里不修改远端 SUB2API 的账号、分组、调度或数据库，因此也不声称掌握其真实上游账号和内部重试。

## SIWC 独立登录

```sh
excel-codex research login-siwc
excel-codex research prepare --out studies/siwc-01 --routes siwc,codex-http --max-calls 6
```

只有显式 `login-siwc` 才打开浏览器。新注册使用动态 client ID、PKCE、state、nonce；回调校验已签名 ID token 的
issuer、audience、有效期、nonce 和选中身份。返回账号不会悄悄替换已选注册。
没有 `chatgpt.tokens.use.direct` 授权时只保存登录，推理保持不可用，不尝试用普通 Codex token 代替。
Unix 文件只允许所有者读取；Windows 额外使用当前用户的 DPAPI 加密。凭据与稳定 host ID 保存在状态目录的 `channels/` 下，
通过 `--auth-file` 可选择独立账号记录。不会自动刷新 SIWC token；过期后重新执行登录命令。
官方文档：[注册与登录](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)、
[模型与推理](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)。

Codex CLI 对照使用一个受保护的临时 home，只复制所选账号运行所需的 access/ID token，不复制 refresh token，
不继承用户配置、项目规则、其他 API key 或应用设置；运行结束/取消后清理。报告注明 CLI 内部调用和响应模型未经独立背书。
原始回答、加密推理及凭据只在运行内存中用于续轮与评分；持久报告保留分数、回答哈希、用量和协议元数据。

## 应用验证过的路线与恢复

```sh
excel-codex check-route --route codex-ws
excel-codex --route codex-ws
excel-codex desktop --route codex-ws
excel-codex restore
```

切换路线后新开对话。CLI 启动只使用临时参数；桌面模式沿用配置备份与退出恢复，`restore` 可单独恢复。
SUB2API sidecar 同样支持 `EXCEL_BRIDGE_ROUTE=codex-ws`，凭据同步仍使用 `push-session --login codex`。
研究报告不会自动替你改默认通道、切账号或调整服务端账单。

## English quick start

`codex-channels` is an offline-by-default entrypoint. Run `prepare`, inspect `request-diff` and `inventory`, then explicitly `run` and `report`.
The six adapters are Codex HTTP, Codex WebSocket, official CLI, separately authorized SIWC, an explicitly configured Responses-compatible service,
and strict text-only BPS. Native HTTP/WS support fixture tool continuation; CLI is a separate text baseline. Production Excel compatibility remains opt-in.

Studies freeze tasks, order, model, effort, credential references and a total submission cap. Reservations are durable before dispatch.
Resume never replays submitted work; crashes keep unknown completions charged. Protocol failures stop that channel for the study.
Reports separate protocol availability, deterministic-oracle accuracy and matched native pairs. Gateway/CLI internals and SIWC workspace equivalence
are not attested. Limits bound client submissions and waiting, not remote token usage. No general quality-restoration claim is made.

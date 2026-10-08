原生 Codex 渠道与协议验收 · Native Codex route and acceptance checks

## 0.6.0

- 默认使用 Codex HTTP 和本机 `codex login` 凭据；旧 BPS 适配通过 `--route excel` 显式保留。
- 原生工具、提示词、图片输入、推理参数和加密历史直接转发。原生请求失败不重试、不自动换渠道或删历史。
- 启动时读取当前账号的原生模型目录，使用其上下文限制和工具能力。旧的 Excel 长上下文别名不用于原生路线。
- 新增 `check-route`：最多两次真实推理，验收工具调用和续轮；提交前记账，失败即停止，保存不含凭据和任务正文的记录。
- 本地服务及 SUB2API sidecar 都支持固定选择渠道，响应头标记实际路线。SUB2API 原生模式需使用新版 `push-session --login codex`。
- 原生 HTTP 适配暂不提供独立生图、服务端会话、SIWC 或 WebSocket；没有能力恢复或质量优势承诺。

升级后完全退出并重启 Codex，为新渠道开始新对话。旧 BPS 加密历史可能无法跨后端续接。
需要旧行为时，设置 `EXCEL_BRIDGE_ROUTE=excel` 或传入 `--route excel`。

## English

- Native Codex HTTP is now the default, using the local ChatGPT login maintained by Codex. Select `--route excel` for legacy BPS behavior.
- Native tools, instructions, image inputs, reasoning parameters and encrypted history pass through. Native inference is never retried, redirected to another channel or repaired by deleting history.
- Model menus use the selected account's native catalog and context limits. Excel long-context aliases are rejected in native mode.
- `check-route` verifies a tool call and its continuation with at most two inference submissions, reserved before dispatch. Receipts exclude credentials, prompts, answers and encrypted reasoning.
- Local and SUB2API servers select one explicit route. Native sidecar imports require the current `push-session --login codex` command.
- Standalone image generation, server-side conversation state, SIWC and WebSocket are not included in the native HTTP adapter. No quality-advantage claim is made.

Fully quit and reopen Codex after upgrading, then start a new conversation for the new route.
For the previous behavior, set `EXCEL_BRIDGE_ROUTE=excel` or pass `--route excel`.

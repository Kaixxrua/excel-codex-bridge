Codex Channels 0.6.0 · 多通道研究与原生桥接

项目从单一 Excel/BPS 适配扩展为明确选择通道、冻结实验、记录真实终态的研究工具。旧仓库名、包名和启动器保留兼容。

- 六种研究适配：Codex HTTP、Codex WebSocket、官方 Codex CLI、独立授权的 SIWC、显式配置的 Responses 兼容服务、严格文本 BPS。
- 完整流程：prepare → request-diff / inventory → run → report，支持 smoke / screen / confirm 与跨时间复验。
- 题目、顺序、模型、档位、凭据引用和预算冻结；提交前持久记账。中断保留次数，续跑不重放，协议失败停止该通道，账号变化拒绝混入旧研究。
- 固定答案、只读 SQL 与合成工具评分；报告区分协议可用率、答题得分、配对结果和身份缺口。原始回答、加密推理和凭据不进入报告。
- 日常桥接默认原生 HTTP；新增 --route codex-ws。CLI、桌面模式与 SUB2API sidecar 都能明确选择 WS，上游失败不自动切回 HTTP。
- 新增 codex-channels 启动入口和 Linux x64 可执行包，提供 Windows x64、macOS arm64/x64、Linux x64 包及 SHA-256。

真实验收中，原生 HTTP/WS 的同题文本与工具续轮均通过，得分持平；官方 CLI 文本基线通过。较早一轮 WS 返回错误，失败记录保留。BPS 返回 403；SIWC 缺少本机 direct 授权，未进行真实推理。Responses 兼容服务使用本地合成上游做端到端验证。这些结果不证明一般质量提升或能力恢复。

迁移：升级后退出并重开 Codex，为不同通道新开对话。旧 Excel/BPS 行为用 --route excel 或 EXCEL_BRIDGE_ROUTE=excel。研究入口默认只查看通道，只有显式 run 才消费推理额度；CLI/中转的内部重试不属于可核验的客户端提交上限。

详见包内 docs/channels.md。SIWC 必须通过独立浏览器授权取得 direct 权限，不接受普通 Codex token 代替。研究不会自动改动生产路由、远端账号池或账单。

## English

Codex Channels adds six explicit research adapters, frozen synthetic fixtures, durable submission budgets, account binding, resumable runs and paired reports. Native HTTP remains the default production bridge; select --route codex-ws for a WebSocket upstream or --route excel for legacy BPS. Existing package names remain compatible.

Native HTTP/WS text and tool-continuation trials passed with tied fixture scores; the official CLI text baseline passed. An earlier WS failure remains recorded. BPS returned 403, SIWC was not live-tested without its separate direct grant, and the compatible Responses adapter passed against a local synthetic service. Protocol availability is not a general model-quality claim.

Packages cover Windows x64, macOS arm64/x64 and Linux x64, with SHA-256 files. Start a new conversation when switching channels. See docs/channels.md for credential handling, supported capabilities and accounting limits.

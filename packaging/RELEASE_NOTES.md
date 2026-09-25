SUB2API 接入与 Docker 部署 · SUB2API integration and Docker deployment

> **非官方项目**，与 OpenAI、Microsoft 无关联。使用加载项后端可能违反 OpenAI 服务条款，风险自负。
> **Unofficial.** Not affiliated with OpenAI or Microsoft. Using the add-in's backend may violate OpenAI's terms; use at your own risk.

## 变化

- **可选 SUB2API 网关**：独立 sidecar 提供 `/v1/models` 和 `/v1/responses`，复用现有流式响应、工具调用和图片适配，无需修改 SUB2API 源码。原有本机 CLI / 桌面模式不变。
- **Docker Compose 部署**：非 root、只读文件系统、独立 API / 管理密钥、Docker secrets，默认不发布宿主机端口。
- **SSH 会话同步**：`excel-sub2api push-session --ssh operator@your-vps` 将本机 Excel 登录态经 SSH 标准输入送入服务端内存；支持 `--watch 60` 定时同步以及状态查询、清除。
- **安装包也能使用**：Windows 运行 `excel-codex.exe sub2api --help`，macOS 运行 `./excel-codex sub2api --help`。从源码安装后另提供 `excel-sub2api` 命令。
- **中英文部署文档**：包含 SUB2API OpenAI / API Key 账户配置、passthrough、Docker 网络、密钥权限和 SSH 同步步骤。

## 同时包含

- 网络失败和超时提示区分连接、发送、等待响应与连接池阶段，更便于定位网络或代理问题。
- `gpt-6-astra-excel` 不再标记为实验性模型。

## 安全边界与限制

- 远程模式必须主动启用；它会把 Excel 登录凭据发送到你指定的服务器，**仅对你完全信任的服务器使用**。本机模式的“仅本机 / 仅发往 OpenAI”保证不适用于远程模式。
- 管理接口仅接受真实 loopback 来源和单独的管理密钥；会话不落盘，容器重启后须重新同步。请勿公开密钥或将其提交到仓库。
- 本版仅提供 Responses 协议；不提供 Chat Completions、Messages、`/responses/compact`、管理面板或多账号池。
- 不自动部署到 VPS，也不自动导出真实登录凭据。自动化验证使用隔离测试会话，不代表真实账号上游调用已验证。

## 下载

- **Windows**：`excel-codex-bridge-0.4.0-windows-x64.zip`。解压后双击 `excel-codex.exe` 打开 Codex CLI，
  双击 `excel-codex-desktop.cmd` 给桌面版用。
- **macOS（Apple 芯片）**：`excel-codex-bridge-0.4.0-macos-arm64.tar.gz`
- **macOS（Intel）**：`excel-codex-bridge-0.4.0-macos-x64.tar.gz`
- **Linux / WSL 或从源码运行**：下载 Source code，使用 `excel-codex.sh`（需要 Python 3.10+）。
- **Linux / VPS 的 SUB2API 部署**：下载本版本 Source code，按 [部署文档](https://github.com/Kaixxrua/excel-codex-bridge/blob/v0.4.0/docs/sub2api.md) 构建 `packaging/sub2api/compose.yaml`。

macOS 推荐在终端用 `curl` 下载，这样不会被"无法验证开发者"拦下（Intel 芯片把 `arm64` 换成 `x64`）：

```
curl -fL https://github.com/Kaixxrua/excel-codex-bridge/releases/download/v0.4.0/excel-codex-bridge-0.4.0-macos-arm64.tar.gz | tar xz
./excel-codex-bridge-0.4.0-macos-arm64/excel-codex status
```

用浏览器下载的，解压后先运行一次 `xattr -dr com.apple.quarantine <解压出的目录>`。

## 注意

- 安装包附带中英文 SUB2API 部署文档。
- **macOS 支持仍是实验性的**：从真实 Mac 版 Excel 读取登录态还没实机验证过，欢迎反馈。
- 程序没有代码签名（Windows 和 macOS 都没有）。可以用同目录的 `.sha256` 文件核对下载是否完整。

交流 QQ 群：966195257

---

## Changes

- **Opt-in SUB2API sidecar** exposing `/v1/models` and `/v1/responses`, with the existing streaming, tool-call and picture adapters. No SUB2API source changes; the default local CLI / desktop workflow is unchanged.
- **Docker Compose deployment** with a non-root, read-only container, separate API/admin keys, Docker secrets, and no published host ports by default.
- **SSH session sync** via `excel-sub2api push-session --ssh operator@your-vps`. Credentials travel over SSH stdin into server memory; `--watch 60`, status and clear commands are available.
- **Included in desktop packages**: `excel-codex.exe sub2api --help` on Windows, or `./excel-codex sub2api --help` on macOS. Source installations also provide the `excel-sub2api` entrypoint.
- Bilingual deployment guides covering SUB2API OpenAI / API Key accounts, passthrough, private networking, secret permissions and SSH sync.

## Also included

- Network errors and timeouts identify the failed stage: connect, send, waiting for a response, or connection pool.
- `gpt-6-astra-excel` no longer carries the experimental label.

## Security and limitations

- Remote mode explicitly transfers Excel credentials to the server you select. **Use only a server you fully trust.** The default mode's local-only / OpenAI-only guarantees do not apply to remote mode.
- Administration requires a real loopback peer and a separate admin key. Sessions are memory-only and must be synced again after a container restart. Never publish credentials.
- Responses only: no Chat Completions, Messages, `/responses/compact`, dashboard or multi-account pool.
- No automatic VPS deployment or export of real credentials. Automated checks use isolated test sessions, not authenticated upstream inference.

## Download

- **Windows**: `excel-codex-bridge-0.4.0-windows-x64.zip`. Double-click `excel-codex.exe` for the
  Codex CLI, or `excel-codex-desktop.cmd` for the desktop app.
- **macOS (Apple silicon)**: `excel-codex-bridge-0.4.0-macos-arm64.tar.gz`
- **macOS (Intel)**: `excel-codex-bridge-0.4.0-macos-x64.tar.gz`
- **Linux / WSL, or from source**: download the source code and use `excel-codex.sh` (Python 3.10+).
- **Linux / VPS with SUB2API**: download this release's source and follow the [deployment guide](https://github.com/Kaixxrua/excel-codex-bridge/blob/v0.4.0/docs/sub2api.en.md).

On a Mac, downloading with `curl` avoids the "developer cannot be verified" block (Intel: replace
`arm64` with `x64`):

```
curl -fL https://github.com/Kaixxrua/excel-codex-bridge/releases/download/v0.4.0/excel-codex-bridge-0.4.0-macos-arm64.tar.gz | tar xz
./excel-codex-bridge-0.4.0-macos-arm64/excel-codex status
```

If you downloaded with a browser, run `xattr -dr com.apple.quarantine <extracted folder>` once.

## Notes

- Packages include bilingual SUB2API deployment guides.
- **macOS support is still experimental**: reading the sign-in from a real Mac Excel has not been
  verified on hardware yet; feedback welcome.
- The programs are not code-signed (neither Windows nor macOS). Check downloads against the
  `.sha256` files next to them.

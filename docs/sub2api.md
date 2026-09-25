# SUB2API 插件：Excel 独立上游

[English](sub2api.en.md)

这是可选 sidecar，不是 SUB2API 核心补丁，也不是 Microsoft Office 安装插件。
复用本仓库的 Excel Responses/SSE、工具调用和图片适配；保留原有本机模式。

```text
Codex / Responses 客户端 → SUB2API → excel-sub2api:8000 → bps.openai.com
                                            ↑
已登录的电脑（Codex 或 Excel）→ SSH → docker exec → 回环管理端点 → 内存会话
```

## 安全边界（先读）

- **只有运行 `push-session` 才会传输会话。** 服务端不扫描 Excel 缓存，不做 OAuth，不处理密码。
- 与本地模式不同，目标服务器的管理员能够访问会话和请求。只连你自己控制、信任的服务器，
  使用自己的账号并遵守相关服务条款；本工具不会绕过套餐限制，不保证上游长期兼容。
- 每实例只放一份 Excel 会话，面向自己的 SUB2API 实例。不是多账号池，也不承诺多租户隔离。
- 会话仅驻留内存；重启后需重新同步。API key、管理 key 是两份不同的独立密钥，
  都不能填成 ChatGPT token。管理 key 不交给 SUB2API。
- 默认 **不发布宿主机端口**；推理需 API key，管理需真实回环来源 + 管理 key。
  `X-Forwarded-For` 无法伪造回环；浏览器 Origin、WebSocket 均拒绝。
- 不要把 `/admin/session` 接到任何反向代理、SSH HTTP 转发或公网入口。
  管理通过 SSH 执行容器内命令，而不是把远端请求伪装成回环。
- SSH 沿用主机指纹校验，token 走标准输入，不进命令行参数、临时文件或日志。
  API/管理密钥文件必须备份到安全位置，不要提交 Git。

## 1. 在服务器准备部署

需要 Docker Compose、现有 SUB2API Docker 网络、Python 3.10+（初始化密钥）。
在本仓库根目录执行：

```sh
python3 -m venv .venv
.venv/bin/pip install .
.venv/bin/excel-sub2api init-secrets packaging/sub2api/secrets
# Linux Compose 的 file secrets 通常保留源文件 UID/权限；容器运行身份为 10001。
sudo chown 10001:10001 packaging/sub2api/secrets/api-key packaging/sub2api/secrets/admin-key
# init-secrets 创建目录 0700、文件 0600，不覆盖已有文件，也不打印密钥。

cd packaging/sub2api
cp .env.example .env
# 修改 .env 中 SUB2API_NETWORK 为现有网络名（不是 Compose 项目名）。
# 查询：docker inspect <你的SUB2API容器> --format '{{json .NetworkSettings.Networks}}'
docker compose config --quiet
docker compose up -d --build
docker exec excel-sub2api excel-sub2api session-status
```

会话尚未导入时 `configured=false` 正常；`/healthz` 只说明进程存活，不代表能推理。
无需 Redis、Postgres 或新增数据库，不改变现有 SUB2API 编排。

## 2. 在 SUB2API 添加账号

使用管理员 UI 新建 **OpenAI / API Key** 账号：

| 项目 | 值 |
| --- | --- |
| Base URL | `http://excel-sub2api:8000/v1` |
| API Key | `packaging/sub2api/secrets/api-key` 的内容，安全读取，不是管理 key |
| OpenAI 透传 | 开启（账号 extra 中的 `openai_passthrough: true`） |
| 模型 | 从 `/v1/models` 的列表选取，保留 `-excel` 后缀，模型映射保持恒等 |
| 分组 / 并发 / 配额 | 沿用你自己的 SUB2API 规则，不改变订阅本身的限制 |

透传用于保留 Responses 工具、reasoning 和输入结构。客户端仍使用 **SUB2API 自己发的 key**，
不是上述上游 key。模型清单以插件接口为准，不写死复制到独立目录。

如果 SUB2API 拒绝私有地址，要按该版本的文档只授权这个 sidecar 主机/端口的出站访问；
**不要为此全局关闭 SSRF 防护**。这里不自动修改 SUB2API 的安全策略。

## 3. 同步会话（Codex 或 Excel 登录）

先在本机准备好一份 ChatGPT 登录，二选一：`codex login`（Codex 自己的登录，无需 Excel），
或在 Excel 的 ChatGPT 加载项里登录。免安装版/源码启动器也可通过
`excel-codex.exe sub2api ...` / `excel-codex.cmd sub2api ...` 使用同一套子命令。
下面为安装 Python 包后的写法：

```sh
# 先手动 SSH 一次，核验主机指纹、配置密钥登录以及 Docker 权限。
ssh operator@your-vps

# 在装有 Excel 或已 `codex login` 的电脑上执行，而不是在 VPS 上执行。
excel-sub2api push-session --ssh operator@your-vps
# 无 Excel 的机器（比如服务器），显式只用 Codex 的登录：
excel-sub2api push-session --ssh operator@your-vps --login codex
# 如果 Docker 需要非交互 sudo：
excel-sub2api push-session --ssh operator@your-vps --sudo
# 每分钟同步；适合会话刷新，以及服务端重启后的重新导入。Ctrl+C 停止。
excel-sub2api push-session --ssh operator@your-vps --sudo --watch 60
```

`--login auto`（默认）先试 Codex 的登录、不行再用 Excel 的；`--login codex` / `--login excel` 只用其一，
也可用环境变量 `EXCEL_BRIDGE_LOGIN`。**这条命令会把你选中的那份登录发往目标服务器**——包括用 Codex
登录时，Codex 的 token 也会离开本机去到你指定的服务器；只连你自己信任的机器。

可用参数：`--ssh-port 2222`、`--identity-file <私钥路径>`、`--container <容器名>`、
`--webview-dir <Microsoft/Office目录>`、`--login <auto|codex|excel>`。原始 IPv6 目标请在 SSH config
中定义别名。同步命令不会自动打开 Excel，也不会刷新/延长任何 token；需要时用 `codex login`（Codex 那份）
或 `excel-codex login` / 手动打开 Excel 加载项重新登录。监视模式会在失败时继续重试，但不会打印请求内容。

## 接口与限制

| 接口 | 凭证 | 作用 |
| --- | --- | --- |
| `GET /healthz` | 无 | 仅存活状态，不返回会话/账号 |
| `GET /v1/models`（或 `/models`） | 上游 API key | Excel 模型列表 |
| `POST /v1/responses`（或 `/responses`） | 上游 API key | 流式/非流式、工具、图片 |
| `GET/POST/DELETE /admin/session` | 回环 + 管理 key | 状态/导入/清除 |

不提供 Chat Completions、Anthropic Messages、`/responses/compact`、WebSocket、管理面板。
请求与解压后体积均限 64 MiB，管理请求限 64 KiB。上游 401/403/429、`Retry-After`
沿用桥接错误语义；token 过期后重新登录并同步即可。不要启动多个 Uvicorn worker
共享同一端口，因为不同进程的内存会话不会同步。

```sh
docker exec excel-sub2api excel-sub2api session-status
docker exec excel-sub2api excel-sub2api clear-session
# 停止并移除这个插件，不触碰原 SUB2API 服务/数据库：
docker compose -f packaging/sub2api/compose.yaml down
```

非 Docker 运行可设置 `EXCEL_SUB2API_API_KEY_FILE` / `EXCEL_SUB2API_ADMIN_KEY_FILE`
后运行 `excel-sub2api serve --host 127.0.0.1`；也支持无 `_FILE` 的环境变量，但二者不能混用。
可选 `EXCEL_BRIDGE_PROXY` 只用于 OpenAI 出站，必须是容器能访问的代理地址。

## 开发与验证

```sh
python -m pytest -q
python -m pytest tests/test_sub2api.py -q
# 使用一次性独立网络/容器，自动清理；不要传入生产网络。
docker build -f packaging/sub2api/Dockerfile -t excel-sub2api:test .
python tests/e2e/sub2api_smoke.py --image excel-sub2api:test
```

自动测试使用合成会话和 MockTransport，不使用真实 ChatGPT token，不消耗订阅额度。
沿用本仓库 Unlicense；仅通过 HTTP 对接 SUB2API，没有复制或打包其实现。

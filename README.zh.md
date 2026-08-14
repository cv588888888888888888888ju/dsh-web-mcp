# dsh-web-mcp

> 🤖 **AI 辅助生成项目** · 大学生学习项目，代码主要由 LLM 编码智能体（Codex CLI + DeepSeek）在人工指导与审查下生成。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)]()
[![MCP](https://img.shields.io/badge/MCP-stdio-purple.svg)]()

**DeepSeek Harness (DSH) Web UI 的 MCP 桥接器** —— 把 DSH web 的 cordis RPC API（`HTTP /api/<endpoint>`）封装成 stdio 形式的 MCP 工具。任何 MCP 客户端（Hermes、Codex CLI、Claude Desktop 等）都可以驱动 DSH 会话，并享受 **prompt 前缀缓存复用**带来的速度与成本优势。

## 这个项目解决什么问题？

DSH（[deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness)）是 DeepSeek 官方的 agent harness，对 `deepseek-v4-flash`（dsf）等自家模型有**接近 100% 的 prompt 前缀缓存命中率**——同一会话内，system prompt + 工具 schema + 历史对话前缀会反复复用，后续每一轮只需要为「新增的那一点点内容」付费，又快又省。

但 DSH 默认只能通过 Web UI 或 CLI 交互，无法被其他 agent 工具链调用。这个项目就是把 DSH 的能力**以 MCP 标准协议开放出来**，让 Codex / Hermes 直接驱动 DSH 会话。

## 工具清单

八个工具，全部基于 DSH web 的 cordis RPC（HTTP `POST /api/<endpoint>`）：

| 工具 | 用途 |
|---|---|
| `dsh_list_workspaces` | 列出当前 DSH web UI 已知的所有工作区。 |
| `dsh_create_session` | 绑定一个目录（缺失时自动创建工作区），创建会话并一次完成模型选择。返回 `sessionId`。 |
| `dsh_send_message` | 发送 prompt 并**阻塞等待**该轮 assistant 回复完成。返回回复文本 + 本轮 token 用量（含 `cacheReadTokens` / `cacheWriteTokens`）。agent 请求权限时通过 MCP **sampling 回调**代为决策（见下），turn 继续跑完。 |
| `dsh_wait_turn` | **不发新 prompt**，只等待当前未完成的 turn 结束。答完 pending 审批后用它续等。 |
| `dsh_list_pending_approvals` | 列出仍未决的审批请求（`approvalId`、`toolName`、`callId?`、`reason?`、`rpcId`），可按 `session_id` 过滤。 |
| `dsh_respond_approval` | 答复一个 pending 审批（`allowed-once` / `rejected`）；返回 DSH 回执（`accepted: true` = 已被消费）。 |
| `dsh_get_session_stats` | 获取缓存投影：`tokenUsage`、`sessionStats`、`contextPressure`。 |
| `dsh_resume_session` | 校验会话仍存活并返回当前模型；后续 `dsh_send_message` 会复用 prompt 前缀。 |

## 权限审批回调（approval callback）

DSH 的 agent 在执行敏感操作（如把 sandbox 升级到 `danger-full-access`）前会请求权限：会话日志里出现 `approval/asked`，同时 DSH web 事件流里出现可答复的 `approval/requested` 帧。本桥把它变成**双轨回调**：

1. **sampling 回调（默认，Hermes CN Desktop ≥ 0.18 可用）**：`dsh_send_message` / `dsh_wait_turn` 等待 turn 期间，server 向 MCP 客户端发送 `sampling/createMessage` 请求，说明工具名与 DSH 给的原因；客户端回答 `allowed-once` / `rejected` 后，桥把结果经 `POST /api/respond` 回传 DSH，turn 继续跑。Hermes 原生支持（`sampling` 默认开启，见其 *Native MCP* 文档）。sampling 必须在 MCP 请求上下文内调用，因此只在真实 MCP 传输下生效。
2. **工具兜底（任何 MCP 客户端可用）**：给 `dsh_send_message` 传 `auto_respond_approvals=false`（或 sampling 不可用/失败），调用会**立即返回** `awaitingApproval: true` + `pendingApprovals`，不再傻等。调用方（或人工）随后决策：

   ```text
   dsh_send_message(...)                  -> {"awaitingApproval": true, "pendingApprovals": [...]}
   dsh_respond_approval(session_id, <approvalId>, "allowed-once")  -> {"accepted": true}
   dsh_wait_turn(session_id, ...)         -> 正常 turn 结果
   ```

   `dsh_list_pending_approvals` 随时可列出仍未决的审批（DSH 事件流在连接时会**重放**所有未答复的审批）。

**wire 事实（已对 DSH 源码 + 实测确认）**：答复必须 echo `approval/requested` 帧的 `rpcId` —— 它是宿主 pending 表新建的 UUID，**不是**审计用的 `approvalId`；payload 里必须带上匹配的 `approvalId`。事件流是 **WebSocket**（`GET /api/events.mux`，普通 HTTP 会返回 426），所以 `websockets` 是硬依赖。

## 环境要求

- Python 3.10+
- 一个正在运行的 `dsh web` 实例：`http://127.0.0.1:3080`（可用环境变量 `DSH_BASE_URL` 覆盖）
- `uv`（推荐）或 `pip`
- `websockets`（`uv sync` 自动安装；审批事件流需要）

## 安装

```powershell
git clone https://github.com/cv588888888888888888888ju/dsh-web-mcp.git
cd dsh-web-mcp
uv sync
```

作为 stdio MCP server 直接运行：

```powershell
uv run dsh-web-mcp
```

## 接入方式

### 接入 Hermes（Agent）

用 `hermes mcp add` 注册：

```powershell
hermes mcp add dsh --command uv --args --directory C:\Users\chenty\Documents\feishu-bot\dsh-mcp run dsh-web-mcp
```

> ⚠️ **踩坑记录（实测）**：
> - `--env DSH_BASE_URL=...` 这个参数会被 dsh-web-mcp 的 argparse 当成自己的参数而报错（`unrecognized arguments: --env ...`），导致连接失败。**DSH_BASE_URL 请用系统/用户级环境变量设置**，不要在 `hermes mcp add` 里传 `--env`。
> - 注册成功后**必须新开一个会话**才生效（配置在会话启动时加载）。
> - 注册时若提示 `Enable all 8 tools? [Y/n/select]`，输入 `Y` 回车确认。

验证是否接入成功（新会话中问 agent）：列一下可用的 `dsh_*` 工具——应看到 8 个工具。

### 接入 Codex CLI

编辑 `%USERPROFILE%\.codex\config.toml`（其他系统为 `~/.codex/config.toml`）：

```toml
[mcp_servers.dsh]
command = "uv"
args = ["--directory", "C:\\path\\to\\dsh-web-mcp", "run", "dsh-web-mcp"]

# 可选，缺省为 http://127.0.0.1:3080
[mcp_servers.dsh.env]
DSH_BASE_URL = "http://127.0.0.1:3080"
```

重启 Codex CLI 后，八个 `dsh_*` 工具会出现在内置工具旁边。

### 前提：DSH web 必须先跑着

两种 MCP 接入方式都要求 DSH web UI 在监听 3080 端口：

```powershell
dsh web --port 3080
```

## 配置

环境变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DSH_BASE_URL` | `http://127.0.0.1:3080` | DSH web 地址。 |
| `DSH_TIMEOUT_S` | `60` | 单请求超时秒数（放宽一点，一轮 LLM 可能要 ~30s）。 |
| `DSH_MCP_LOG` | `INFO` | Python `logging` 级别（`DEBUG` 可看 wire 级流量）。 |

CLI flag 与环境变量一一对应：`--base-url`、`--timeout`、`--check`。

## 自检（probe）

`probe.py` 是开发者侧的冒烟测试，会端到端调用全部八个工具，**并包含审批链路**（真实触发一次审批 → `dsh_respond_approval` 返回 `accepted: true` → `dsh_wait_turn` 等到 turn 跑完）：

```powershell
uv run python probe.py
```

预期输出（每步一个 JSON）：

```json
{
  "ok": true,
  "step": "send_message",
  "reply_contains": "TASK_OK",
  "cacheReadTokens": 8192
}
```

审批相关步骤：`send_message_awaiting_approval`、`respond_approval`（`accepted: true`）、`wait_turn_after_approval`（`reply_contains: "APPROVAL_OK"`）。

`cacheReadTokens > 0` 即证明缓存复用生效。

## 故障排查

- **DSH web 不可达** —— server 能启动，但每个工具都返回 `{"ok": false, "error": "DSH web not reachable at ..."}`。确认 `dsh web` 正在运行（`dsh web --port 3080`）。
- **DSH schema 漂移（rc.X → rc.Y）** —— 未知字段错误以 `{ok: false, error: "dsh returned <code>: <msg>"}` 形式返回。`models.py` 中的模型 schema 刻意设为 `extra="allow"` 以便透传新增字段；解析异常请对着 `models.py` 报 issue。
- **Prompt 超时** —— `dsh_send_message` 默认 `timeout_s=120`；prompt 较长时请调大重试。无人答复的审批也会挂住 turn：用 `dsh_list_pending_approvals` / `dsh_respond_approval`（或让 DSH web UI 里的人类批准）后接 `dsh_wait_turn`。
- **sampling 不可用** —— 在 MCP 请求上下文之外（如 `probe.py`）或客户端未实现 `sampling/createMessage` 时，`dsh_send_message` 回退为返回 `awaitingApproval: true` + `pendingApprovals`；走上面的工具兜底即可。

## 为什么值得用（缓存原理）

默认情况下 Codex CLI 直连 OpenAI / Azure provider。当通过 MCP 路由到 DSH 时，DSH 内的 `deepseek-v4-flash` preset 会让 system prompt + 工具 schema + 会话前缀保持缓存，因此同一会话内的每一轮都能读到 8K+ 的缓存 token，只有新 prompt 与新回复按未缓存计费——详见 `dsh_send_message` 返回的 `tokenUsage.cacheReadTokens` 实测值。

## 项目背景

本项目是一位本科生探索 agent 工具链的学习项目。绝大部分代码由 LLM 编码智能体（Codex CLI + DeepSeek）在人工指导下生成；每一行都经过人工审查，发布前做了端到端行为验证。作者经验有限，bug 可能存在——欢迎提 issue。

## 状态

Pre-release。API 面跟随 DSH 0.1.0-rc.6 schema（`rpc-map.d.ts`）；如果升级 DSH，请从源码重新生成。

## License

[MIT](LICENSE)

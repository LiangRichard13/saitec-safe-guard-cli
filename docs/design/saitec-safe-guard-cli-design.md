# Saitec Safe CLI — 设计文档

- **日期**：2026-08-14
- **状态**：待评审（Draft）
- **项目目录**：`C:\Users\Administrator\Desktop\projects\saitec-safe-guard`

## 1. 背景与目标

在单位内部，我们需要对"调用大模型 API 时的请求与响应"做安全检测。本工具是一个**命令行 CLI**，运行在用户本机（单机自用），通过**反向代理**的方式，让大模型 API 的请求与响应都经过本工具，从而采集到明文内容，缓存后定时上报给单位的安全检测服务器，并记录返回的检测结果。

### 核心目标

1. 起多个监控服务，每个服务对应一个固定上游大模型端点、一个本地端口。
2. 采集请求与响应的明文，理解三种 LLM 协议，重组出结构化、完整的对话内容。
3. 缓存记录，按固定间隔批量上报到单位检测服务器（`X-API-Key` 认证）。
4. 记录检测结果到本地，可查询。

### 明确非目标（YAGNI）

- **不做多机批量部署**：单机自用，不考虑 CA 证书批量分发、静默服务化、集中配置下发、升级管理。
- **不做 CA 证书 / MITM / TLS 解密**：本地端口走 http 明文，无需授权、无需安装自签根证书。
- **不做 TUN / 透明代理**：不拦截网络层流量。
- **不做通用正向代理**：不按域名识别任意流量。
- **v1 只适配三种端点**：OpenAI Chat Completions、OpenAI Responses、Anthropic Messages。其他厂商 SDK/CLI 留到后续。

## 2. 可行性结论

**可行，无原理性障碍。**

主要工作量与风险集中在两处：

1. **三种 SSE 流式格式的解析重组**：OpenAI Chat Completions / OpenAI Responses / Anthropic Messages 的流式 chunk 结构各不相同，需各写一个 adapter，把碎 delta 重组成完整内容，并正确提取 usage。
2. **三种端点流式 chunk 实际形态**：以真实流量为准，而非仅凭文档。

> 客户端侧的 http 兼容性问题已经确认：Claude Code 被 ccswitch 在 `127.0.0.1:15721` 上验证可行，Codex 也支持 http 格式（结构性前置已打通）。

## 3. 关键技术决策

| 维度 | 决定 |
|---|---|
| 技术栈 | Python + `aiohttp`（异步 HTTP/代理/流式）+ `typer`（CLI） |
| 架构 | 单进程 + asyncio 单事件循环 + 多端口 |
| 代理形态 | 反向代理，一个端口 → 一个固定上游 |
| 本地协议 | http 明文（`http://127.0.0.1:<port>`），无需 CA/MITM/授权 |
| 部署 | 单机自用 |
| 覆盖客户端 | 自研 Python 调用、Claude Code、Codex / OpenAI CLI |
| 端点适配（v1） | OpenAI Chat Completions、OpenAI Responses、Anthropic Messages |
| 上报认证 | `X-API-Key` header，使用前先 `init` 配置 |
| 结果返回 | 同步返回结论（字段可配置，接口定后对齐） |
| 敏感信息 | 默认脱敏 + body 记录开关 |
| 配置格式 | JSON |
| 结果存储 | SQLite |

## 4. 工作原理：为什么本地 http 明文即可，且厂商 https 不冲突

一次请求被拆成两段独立的连接：

```
[客户端 SDK]  ──① http://127.0.0.1:9001 明文──▶  [CLI 反向代理]  ──② https://api.xxx.com──▶  [大模型厂商]
       ◀──────────────────────────────────────────  ◀──────────────────────────────────
```

- **第①段（客户端 → CLI）**：跑在本机 loopback（127.0.0.1）上，协议由我们自己决定。客户端把 `base_url` 从厂商地址改成 `http://127.0.0.1:<port>`，这一段就是明文，工具能直接读到请求与响应。明文只在本机进程间，不经过网络。
- **第②段（CLI → 厂商）**：由工具用标准 HTTPS + 真实证书转发，厂商侧看到的仍是正常 HTTPS 客户端，安全性不降。

因此，**厂商使用 https 不是障碍**——它由工具在第②段满足。工具本身无需 CA 证书、无需 MITM、无需任何授权。

### 各客户端接入方式

| 客户端类型 | 覆盖端点方式 |
|---|---|
| 自研 Python（OpenAI SDK） | `base_url` 参数 或 `OPENAI_BASE_URL` |
| 自研 Python（Anthropic SDK） | `base_url` 参数 或 `ANTHROPIC_BASE_URL` |
| Claude Code | 环境变量 `ANTHROPIC_BASE_URL` |
| Codex / OpenAI CLI | 环境变量 `OPENAI_BASE_URL` |

> Claude Code 接受 `http://127.0.0.1` 明文已被 ccswitch 案例验证；Codex 也支持 http 格式。两个客户端的 http 兼容性已经确认（流式 chunk 实际形态仍需实测，见 §11）。

## 5. 架构设计

```
                      ┌───────────────────────────────────────────────────────┐
[自研 Python] ──base_url──▶│ 端口 9001 ──▶ 上游 https://api.openai.com           │
[Claude Code] ──ANTHROPIC─▶│ 端口 9002 ──▶ 上游 https://api.anthropic.com        │   一个 Python 进程
[Codex] ──OPENAI_BASE_URL─▶│ 端口 9003 ──▶ 上游 https://api.openai.com           │   (asyncio 单事件循环)
                        └──────────┬────────────────────────────────────────────┘
                                   │ 归一化记录 → 内存队列 + JSONL 落盘
                                   │ 后台定时任务批量 POST ──▶ 单位检测服务器
                                   │ 检测结果 ──▶ SQLite
```

### 组件划分

| 组件 | 职责 | 选型/依赖 |
|---|---|---|
| CLI 层 | 子命令解析、配置加载、启停、查询 | `typer` |
| 代理核心 | 每端口一个反向代理，转发 + 流式透传 | `aiohttp` |
| 协议适配层 | 解析三种协议，重组流式内容，产出归一化记录 | 自定义 adapter |
| 记录器 | 脱敏、写内存队列、异步落盘 | JSONL |
| 上报器 | 定时批量 POST 到检测服务器，重试 | `aiohttp` + asyncio 定时任务 |
| 结果存储 | 检测结果落库、查询 | SQLite |
| 配置 | 多服务、端口、上游、上报目标、频率 | JSON |

每个组件可独立理解与测试：代理核心不知道协议细节；协议适配层不知道上报细节；上报器不知道存储细节。

## 6. 协议适配层

这是本工具的核心增值点。代理底座负责透传，adapter 负责"理解"协议并产出统一记录。三个端点的本质差异在**流式 SSE 格式**和 **usage 位置**：

| 端点 | 路径 | 流式 chunk 结构 | usage 位置 |
|---|---|---|---|
| OpenAI Chat Completions | `/v1/chat/completions` | `data: {...}`，`choices[0].delta.{role,content}`，结尾 `data: [DONE]` | 末个 chunk（需 `stream_options.include_usage`）或非流式响应体 |
| OpenAI Responses | `/v1/responses` | `data: {...}`，靠 `type` 字段区分事件 | `response.completed` 事件 |
| Anthropic Messages | `/v1/messages` | `event: <type>` + `data: {...}`，`content_block_delta.delta.text` | `message_delta` 事件 |

### 归一化记录 schema（上报单元）

```jsonc
{
  "record_id": "550e8400-e29b-41d4-a716-446655440000",   // UUID v4，由 proxy 生成，全链路唯一键
  "service": "openai-chat-completions",                    // 监控实例名（来自配置 services[].name）
  "endpoint_type": "openai-chat-completions",              // 协议类型（三选一：openai-chat-completions / openai-responses / anthropic-messages）
  "upstream": "https://api.openai.com",
  "path": "/v1/chat/completions",
  "timestamp": "2026-08-14T12:00:00Z",
  "elapsed_ms": 812,                                        // 端到端耗时（请求进入 → 响应完成）
  "status_code": 200,
  "error": null,                                            // 失败时的错误描述（上游超时 / 5xx / SSE 解析失败 / TCP 断开等）；成功时为 null
  "request": {
    "model": "gpt-4o",
    "messages": [ { "role": "system", "content": "..." }, { "role": "user", "content": "..." } ],
    "tools": [],
    "raw": {}                                                // 原始 body，受 record_body 开关控制
  },
  "response": {
    "content": "完整响应文本",                                 // 流式 delta 重组后
    "finish_reason": "stop",
    "usage": { "prompt_tokens": 12, "completion_tokens": 34 },
    "raw": null                                               // 受 record_body 开关控制：关时为 null
  }
}
```

**字段语义约束**：

- `record_id`（UUID v4）是**全链路唯一键**，由 `proxy` 端生成，跨 JSONL 上传 + SQLite 落库 + 幂等上报。
- `service` 与 `endpoint_type` **是两个独立字段，不允许互推**：
  - `service` = 监控实例名（来自配置 `services[].name`），例如 `prod-openai-1`、`team-anthropic` 等用户起的别名
  - `endpoint_type` = 协议类型枚举，决定走哪个 adapter
  - 两者允许不同（如同一服务可代理到不同协议），但**保持独立字段以避免歧义**。
- `error` 非空时表示该请求失败，其余字段仍尽量填充（保证记录可被重放 / 分析）。

## 7. 数据流

一次请求的完整生命周期：

1. 客户端 →（http 明文）→ 本地端口。
2. 代理核心接收请求，交给对应 adapter 解析请求（重组 messages / model / tools 等）。
3. 代理核心向上游以 https 转发。
4. 上游响应（可能为 SSE 流式）返回，代理核心**边透传边累积**（见 §8）。
5. 流结束后，adapter 重组完整响应（content / finish_reason / usage），产出归一化记录。
6. 记录器脱敏后写入内存队列，并异步追加到 JSONL 落盘。
7. 上报器按 `report_interval_sec` / `batch_size` 批量 POST 到检测服务器。
8. 收到检测结论，写入 SQLite。

### 关键：同步业务 vs 异步观测（避免双发误解）

本工具的工作链路是**两条完全分离的链路**，不是"一笔请求同时双发"：

- **同步业务链路（透明代理）**：客户端 → CLI → 大模型 → CLI → 客户端。**一笔请求仅一份**，客户端与大模型的对话完全通过本工具完成，与正常调用无感知差异。
- **异步观测链路（旁路采集）**：上面那条同步链路里**采集到的请求/响应**进内存缓存（+ 落盘），由后台定时任务**批量**上传给检测服务器。这条链路与业务链路完全解耦——检测服务器慢、挂了、或网不好，都不会影响客户端和大模型的对话。

任何误以为"一次请求被同时双发到检测服务器 + 大模型"的实现都会破坏客户端体验，并引入不必要的延迟。

## 8. 流式响应（SSE）处理

上游若 `stream: true`，响应不是一次性回来，而是逐条 `data:` 推送。处理原则：

- **边转发边累积**：`async for chunk in upstream_resp` 一边把 chunk 原样写给客户端（保证客户端体验不卡），一边把 delta 拼进 adapter 的 buffer。
- **流结束后重组**：收到终止标记（如 `data: [DONE]` 或 `message_stop` / `response.completed`）后，adapter 从 buffer 重组出完整 `content` 与 `usage`。
- 这是实现上最有技术含量、也最容易踩坑的一处，需单独覆盖测试（三种格式各一组流式样例）。

**异常处理规则**（流式链路是高发故障点，必须鲁棒）：

| 异常 | detection | 行为 |
|---|---|---|
| 上游 TCP 提前断开（流未完成） | 立即结束透传 + 标 `error="stream_incomplete"` | 不抛异常，**最终**调用 `finalize()` 提交 partial 记录 |
| SSE 格式错误（缺 `data:` 前缀、JSON 解析失败） | 该行跳过 + 记 `error_chunk_count` | 不抛异常，透传继续 |
| 上游超时 | 标 `error="upstream_timeout"` + 关闭流 | 同上 |
| `finalize()` 自身异常 | 兜底：提交 raw buffer 为 `content`，`usage = null` | 不抛异常，记录日志 |
| 客户端提前断开 | 关闭上游连接 | 标记流被截断，partial 记录仍上报 |

**关键不变量**：

- `on_stream_chunk` **绝不抛异常**——坏数据进入，partial 记录出来，但绝不破坏透传链路。
- 每条请求**最终**必须调用一次 `finalize()` 提交记录（即使上游中断），保证 `recorder.enqueue()` 一定会被调用。

## 9. 错误处理

- **上游不可达 / 超时**：记录错误状态，向客户端返回 502，该次失败仍进入记录（标记失败原因）。
- **上报失败**：落盘队列保留，指数退避重试，不阻塞代理转发。
- **落盘满 / 损坏**：告警，继续使用内存队列，避免影响主链路。
- 记录与上报是**旁路逻辑**，任何失败都不能打断代理转发本身。

## 10. 敏感信息脱敏

安全检测工具自身不能成为新的泄密源。默认策略：

- 对 `Authorization` / `X-API-Key` / `Cookie` 等敏感 header **自动打码或丢弃**。
- 请求/响应 **body 是否落盘记录做成开关**（`record_body`，默认按服务配置）。
- 归一化记录中 `raw` 字段受开关控制；`messages` / `content` 保留结构化内容供检测。

## 11. 待验证项（编码前需实测）

1. ✅ **Claude Code** 接受 `http://127.0.0.1:<port>` 明文——已验证，参考 ccswitch 在 127.0.0.1:15721 上将 Claude Code（anthropic messages）接入 OpenAI chat completions 的案例。该工具的存在本身就是 Claude Code 接受 http 明文 + 与反向代理协作的活证明。
2. ✅ **Codex / OpenAI CLI** 接受 http 格式——已确认支持。
3. ❓ **三种端点流式 chunk 实际形态**：以真实流量为准，而非仅凭文档。三种端点的 SSE 事件结构（尤其是 OpenAI Responses 的 `type` 枚举、Anthropic Messages 的 `event:` 行）需用真实流量验证，作为 adapter 实现的依据。

> 客户端侧的 http 兼容性已经结构性打通。若未来某个新客户端拒绝 http 明文、强制 https，可针对它单独评估"https + 自签 CA"分支（当前不在 v1 范围内）。

## 12. 配置设计（JSON）

> **默认路径**：`config.json` 默认位于平台用户目录（由 `platformdirs` 解析）：
> - Linux：`~/.local/share/saitec/config.json`
> - macOS：`~/Library/Application Support/saitec/config.json`
> - Windows：`%LOCALAPPDATA%\saitec\config.json`
>
> 覆盖机制见 §16.3 与 §12.1。

```json
{
  "detector": {
    "url": "http://<检测服务器>/detect",
    "api_key": "<X-API-Key>",
    "report_interval_sec": 60,
    "batch_size": 100
  },
  "services": [
    {
      "name": "openai-chat-completions",
      "port": 9001,
      "upstream": "https://api.openai.com",
      "endpoint_type": "openai-chat-completions",
      "record_body": true
    },
    {
      "name": "openai-responses",
      "port": 9002,
      "upstream": "https://api.openai.com",
      "endpoint_type": "openai-responses"
    },
    {
      "name": "anthropic-messages",
      "port": 9003,
      "upstream": "https://api.anthropic.com",
      "endpoint_type": "anthropic-messages"
    }
  ]
}
```

**配置文件权限要求**（**必读**）：

- `config.json` 中 `detector.api_key` 是**明文**，必须限制文件权限：
  - **Linux / macOS**：`chmod 600 config.json`（仅当前用户可读写）
  - **Windows**：`icacls config.json /inheritance:r /grant:r "%USERNAME%:R"`
- `init` 命令在生成 `config.json` 后**自动执行上述权限设置**（如果失败仅警告，不阻断）。
- 非交互模式下传入 `--api-key` 时，**推荐从环境变量 / stdin 注入**，避免 `--api-key` 出现在 shell history 或进程列表：

```bash
# 推荐：从 stdin 读取
read -r -s API_KEY && safe-guard init --api-key "$API_KEY" --detector-url ...

# 或从环境变量
safe-guard init --api-key "$SAITEC_API_KEY" --detector-url ...
```

- `init` 完成后**不应**继续让 `api_key` 留在 shell history，用 `unset API_KEY` 清理。

### 12.1 配置三级覆盖（环境变量 / 命令行）

**优先级从高到低**：

```
命令行参数  >  环境变量  >  config.json（默认 / 持久化）
```

- **配置文件**是 canonical source（持久化默认）
- **环境变量**是覆盖一次进程（CI / 多部署场景友好）
- **命令行**是覆盖一次调用（Agent 自动化）

**字段覆盖矩阵**：

| 字段 | config.json | 环境变量 | 命令行 |
|---|---|---|---|
| `detector.url` | ✓ | `SAITEC_DETECTOR_URL` | `--detector-url` (init / start) |
| `detector.api_key` | ✓ | `SAITEC_API_KEY` | `--api-key` (init only) |
| `detector.report_interval_sec` | ✓ | `SAITEC_REPORT_INTERVAL` | `--report-interval` |
| `detector.batch_size` | ✓ | `SAITEC_BATCH_SIZE` | `--batch-size` |
| `detector.max_queue_size` | ✓ | `SAITEC_MAX_QUEUE_SIZE` | `--max-queue-size` |
| `services[N].port` | ✓ | `SAITEC_<NAME>_PORT` | `--port` |
| `services[N].upstream` | ✓ | `SAITEC_<NAME>_UPSTREAM` | `--upstream` |
| `services[N].endpoint_type` | ✓ | (固定，启动时校验) | (固定) |
| `services[N].record_body` | ✓ | `SAITEC_<NAME>_RECORD_BODY` | `--record-body` |
| `log_level` | ✓ | `SAITEC_LOG_LEVEL` | `--log-level` |
| `config.json` 路径 | (默认 `./config.json`) | `SAITEC_CONFIG` | `--config` |

**环境变量命名约定**：

- 全大写，`SAITEC_` 前缀
- 用 `_` 分隔字段
- service 维度的覆盖用 `SAITEC_<NAME>_XXX` 形式，其中 `<NAME>` 与 `services[N].name` 一致，否则报错

**覆盖语义**：

- **单字段覆盖**：env / cli 只覆盖**字段**，不存在的字段保持原配置
- **service 数组**（`services[]`）**无法用 env / cli 整体替换**，必须改 config.json；单字段覆盖是允许的
- **`record_body` 默认值**：未设置时为 `true`（默认记录 body，但受 `redact_headers` 脱敏）

**runtime 注入时机**（`runtime.start()` 之前）：

```python
# 伪代码
config = load_config_json(path)           # 1. 配置文件
config = apply_env_overrides(config)      # 2. 环境变量
config = apply_cli_overrides(config)      # 3. 命令行（如果指定）
errors = validate_config(config)          # 4. 校验
if errors: exit(1)
```

**来源追踪**：`config list` 命令显示每个字段的最终来源（config / env / cli / default），便于调试。

## 13. CLI 命令（草案）

按四类组织：配置 / 生命周期 / 运维 / 调试，共 13 个命令。

### 13.0 命令清单

**配置类**（3 个）：

```
safe-guard init         交互式 / 非交互式生成 config.json
safe-guard validate     校验 config.json（不启动服务）
safe-guard config       配置管理（get / set / unset / list 子命令）
```

**生命周期类**（5 个）：

```
safe-guard start        启动服务（异步，PID 文件）
safe-guard stop         优雅停止服务（SIGTERM → SIGKILL 兜底）
safe-guard restart      优雅重启（stop + start）
safe-guard status       查询运行状态（各端口 / 队列 / 上报状态）
safe-guard logs         查看日志（--tail N / --follow / --service NAME）
```

**运维类**（3 个）：

```
safe-guard report       查询 SQLite 检测结果
safe-guard redo         手动重报某条记录（绕过游标）
safe-guard purge        清理过期 JSONL + SQLite（--retention-days N）
```

**调试类**（2 个）：

```
safe-guard doctor       自检（端口 / API key / 磁盘 / SQLite / JSONL）
safe-guard tail         实时跟踪事件流（类似 tail -f JSONL）
```

### 13.1 输出契约（Agent 友好）

CLI 不仅是给人看的，**必须可被 Agent 解析**。每条命令遵循三条约：

**1. 双形态输出**：默认人类可读（表格），加 `--json` 切换为 JSON。

```bash
safe-guard status                 # 人类可读表格
safe-guard status --json          # 机器可读 JSON
```

**2. 退出码语义化**：

| 退出码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 用户错误（参数错、配置不存在） |
| 2 | 运行时错误（端口被占用、启动失败） |
| 3 | 检测服务器错误（不可达、4xx/5xx） |
| 4 | 内部错误（未捕获异常） |

**3. `stdout` / `stderr` 分离**：数据 → `stdout`；日志 / 错误 → `stderr`。

**非交互模式**：`init` 默认交互，但支持全非交互参数（Agent 友好）：

```bash
safe-guard init --api-key "$API_KEY" --detector-url "http://detector:8080" --config ./config.json
```

**异步启动**：`start` 立即返回（成功 `exit 0` + PID 文件），Agent 用 `status` 查询运行状态。

> 详细错误输出格式与示例见 `architecture.md` §4 Layer 6"输出契约"。

## 14. 风险与开放问题

- **检测服务器接口未定**：上报请求体字段、检测结果返回字段仍需与检测侧对齐。当前按"同步返回结论 + 可配置"设计。
- **协议随厂商演进**：三种端点格式可能随版本变化，adapter 需可独立维护、易扩展新端点类型。
- **Codex 的 http 兼容性**：Codex 已确认支持 http 格式（结构性前置已打通）。Claude Code 已被 ccswitch 验证（参见 §11）。客户端侧的 http 兼容性不再是风险点。

## 15. 配置管理（config 子命令与环境变量）

本节详细描述 `config` 子命令与配置三级覆盖矩阵（与 §12.1 互为补充）。

### 15.1 `config` 子命令

**`config get <key>`** —— 查看单个字段

```bash
safe-guard config get detector.url
# http://detector:8080

safe-guard config get services.openai-chat-completions.port --json
# {"value": 9001, "source": "config"}
```

**`config set <key> <value>`** —— 修改单个字段（持久化）

```bash
safe-guard config set detector.report_interval_sec 30
# 写入 config.json（自动快照 + 校验）
```

**安全策略**：

- **修改前快照**：保存 `config.json.bak.<timestamp>`
- **修改后立即 `validate`**：校验失败则**回滚**
- **不自动重启**：必须 `safe-guard restart` 才生效
- **`status` 显示**：当前配置 vs 重启后配置（diff）

**`config unset <key>`** —— 清除字段（回退到默认）

```bash
safe-guard config unset services.openai-chat-completions.record_body
# 该字段将使用默认值 true
```

**`config list`** —— 列出所有字段（含来源）

人类可读（默认）：

```
KEY                                       VALUE                      SOURCE
detector.url                              http://detector:8080       config
detector.api_key                          sk-***                     config
detector.report_interval_sec              30                         env (SAITEC_REPORT_INTERVAL)
services.openai-chat-completions.port     9001                       config
```

JSON 形态（Agent 友好）：

```json
{
  "config": {
    "detector.url": {"value": "http://detector:8080", "source": "config"},
    "detector.api_key": {"value": "sk-***", "source": "config"},
    "detector.report_interval_sec": {"value": 30, "source": "env", "env_var": "SAITEC_REPORT_INTERVAL"},
    "services.openai-chat-completions.port": {"value": 9001, "source": "config"}
  },
  "effective_at": "2026-08-14T..."
}
```

### 15.2 字段路径约定

- 用点路径：`detector.url` / `services.<name>.port`
- `services.<name>` 必须与配置文件中的 `name` 一致
- 仅支持**单字段**路径（不支持数组下标 `services[0].port`）

### 15.3 环境变量矩阵（参考 §12.1）

| 字段 | 环境变量 |
|---|---|
| `detector.url` | `SAITEC_DETECTOR_URL` |
| `detector.api_key` | `SAITEC_API_KEY` |
| `detector.report_interval_sec` | `SAITEC_REPORT_INTERVAL` |
| `detector.batch_size` | `SAITEC_BATCH_SIZE` |
| `detector.max_queue_size` | `SAITEC_MAX_QUEUE_SIZE` |
| `services.<NAME>.port` | `SAITEC_<NAME>_PORT` |
| `services.<NAME>.upstream` | `SAITEC_<NAME>_UPSTREAM` |
| `services.<NAME>.record_body` | `SAITEC_<NAME>_RECORD_BODY` |
| `log_level` | `SAITEC_LOG_LEVEL` |
| `config.json` 路径 | `SAITEC_CONFIG` |

### 15.4 边界与限制

- **service 数组无法用 env / cli 整体替换**：必须改 config.json
- **`endpoint_type` 不允许 env / cli 覆盖**：协议类型决定 adapter，运行时改风险大
- **`api_key` 仅在 init 时通过 cli 覆盖**：运行时通过 env 覆盖（避免泄漏命令行历史）
- **`config set` 嵌套字段的写入**：dot 路径解析后写入对应 JSON 层级；如果中间层级不存在则报错（不允许创建新数组）

## 16. 部署路径与目录解析

本节定义 `safe-guard` 安装后的默认路径规则，遵循业界成熟 CLI 的做法（git config / docker context / kubectl 等）。

### 16.1 三种路径策略对比

| 策略 | 默认路径 | 覆盖机制 | 适用 |
|---|---|---|---|
| **跟随项目目录**（最简） | `./config.json` | --config | 开发期 |
| **平台用户目录**（最常用） | platformdirs 默认 | --config / $SAITEC_CONFIG | pip install 后 |
| **XDG 拆分**（Linux 偏好） | config + data 分离 | env vars | 严格备份策略 |

**设计决策**：**采用方案 2（平台用户目录）作为默认 + 方案 3 的覆盖机制**。

### 16.2 默认目录规则

通过 `platformdirs` 库解析跨平台统一路径：

| 平台 | 默认目录 |
|---|---|
| **Linux** | `~/.local/share/saitec/` |
| **macOS** | `~/Library/Application Support/saitec/` |
| **Windows** | `%LOCALAPPDATA%\saitec\` |

目录结构：

```
<config_dir>/
├── config.json          # 主配置文件
├── config.json.bak.*    # config set 时的快照
├── records/             # JSONL 落盘
│   ├── records-2026-08-13.jsonl
│   ├── records-2026-08-14.jsonl
│   └── ...
├── results.db           # SQLite 检测结果库
└── logs/                # safe-guard logs 命令读取
    └── safe-guard.log
```

**关键原则**：

- **数据目录跟随 config 目录**：`records/` / `results.db` / `logs/` 全部在 `config.json` 的**父目录**下
- **统一目录**：不引入 XDG 拆分（避免 macOS/Windows 没有 XDG 的跨平台表达不一致）
- **`platformdirs` 是唯一第三方依赖**（除 aiohttp / sqlite3 / typer 外）

### 16.3 覆盖机制

**优先级**：

```
显式 --config PATH  >  $SAITEC_CONFIG  >  平台默认目录
```

```bash
# 1. 默认（平台用户目录）
safe-guard init
# → Windows: C:\Users\<user>\AppData\Local\saitec\config.json
# → Linux:   /home/<user>/.local/share/saitec/config.json
# → macOS:   /Users/<user>/Library/Application Support/saitec/config.json

# 2. 环境变量覆盖
SAITEC_CONFIG=/etc/saitec/prod.json safe-guard start

# 3. 命令行覆盖
safe-guard --config /etc/saitec/prod.json start
```

**首次 init 自动创建**：当平台默认目录不存在时，`safe-guard init` 自动创建 `config_dir/` + 子目录 `records/` / `logs/`。

### 16.4 与现有三级覆盖的衔接

路径解析**与 §12.1 的三级字段覆盖独立**：

- `--config PATH` / `$SAITEC_CONFIG` → 决定**配置文件位置**（路径层）
- `--api-key` / `$SAITEC_API_KEY` / config.json → 决定**字段值**（字段层）

`config list` 显示**每个字段**的来源（config / env / cli），与路径解析无关。

### 16.5 实施要点

新增模块 `core/paths.py`：

```python
from platformdirs import user_data_dir

APP_NAME = "saitec"

def resolve_config_dir() -> Path:
    """解析配置目录：$SAITEC_CONFIG 父目录 / --config 父目录 / platformdirs 默认"""
    return Path(user_data_dir(APP_NAME, appauthor=False))

def resolve_config_path() -> Path:
    """解析 config.json 完整路径"""
    return resolve_config_dir() / "config.json"

def resolve_data_dir() -> Path:
    """数据目录（records / db / logs），跟随 config_dir"""
    return resolve_config_dir()

def ensure_dirs() -> None:
    """首次 init 时调用，确保所有子目录创建（mode 0o700）"""
    d = resolve_config_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    (d / "records").mkdir(exist_ok=True, mode=0o700)
    (d / "logs").mkdir(exist_ok=True, mode=0o700)
```

**依赖**：`platformdirs` （新增第三方依赖，但极小、广泛使用）。

### 16.6 多项目场景

同一台机器部署多个项目时，用 `--config` 区分：

```bash
# 项目 A
safe-guard --config ~/.saitec-prod.json start

# 项目 B
safe-guard --config ~/.saitec-staging.json start
```

**注意**：每个 config 拥有独立的 `records/` / `results.db` / `logs/`（数据跟随 config 目录）。

### 16.7 已知缺口

- **跨平台权限**：Windows 下 `mode=0o700` 不完全等价，需要 `icacls`（见 §12 文件权限要求）
- **首次 install**：如果 platformdirs 默认目录权限不对（如 umask 太宽松），需要 `ensure_dirs()` 后做权限检查
- **路径迁移**：v1 不提供从 `./config.json` 到平台目录的迁移工具，留作 v2 运维命令（`migrate`）

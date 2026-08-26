# Saitec Safe Guard CLI — 用户手册

> 反向代理 CLI：把大模型 API 调用转一道，便于安全检测与审计。

本手册面向**使用者**——CLI 已可用，你可以跟着"快速上手"5 分钟跑通；命令详解、配置、集成、排错在后面章节。

---

## 目录

1. [它解决什么问题](#1-它解决什么问题)
2. [安装](#2-安装)
3. [快速上手（5 分钟）](#3-快速上手5-分钟)
4. [完整命令参考](#4-完整命令参考)
5. [配置文件详解](#5-配置文件详解)
6. [集成到你的客户端](#6-集成到你的客户端)
7. [排错手册](#7-排错手册)
8. [安全注意事项](#8-安全注意事项)
9. [进阶用法](#9-进阶用法)

---

## 1. 它解决什么问题

当你用 Claude Code / Codex / 自写脚本调用大模型 API 时，所有请求和响应都会经过公网到大模型厂商。安全团队需要把这些流量**记录下来**并按周期上报到内部检测服务器做合规审计——这就是 `safe-guard` 的工作。

**它做什么**的
- 在本地监听 3 个端口（默认 9001/9002/9003），分别代理到 OpenAI Chat Completions、OpenAI Responses、Anthropic Messages
- 记录每次请求/响应到 JSONL（按天分片，崩溃恢复源）
- 周期（默认 60 秒）上报归一化记录到检测服务器，带 `X-API-Key` 鉴权
- 把检测结果（`clean / violation / risk_level`）写入本地 SQLite，方便查询

**它不做什么**：
- ❌ 不做 CA/MITM（用户端改请求基址到 localhost 即可，无需配置证书）
- ❌ 不修改请求/响应内容（透明转发）
- ❌ 不阻止流量（即使 detector 报 violation，请求依然会被发到上游）

---

## 2. 安装

### 2.1 前置条件

- **Python ≥ 3.10**（建议 3.11/3.12）
- **操作系统**：Windows / macOS / Linux 均可（推荐 Windows 11 或 macOS 13+）
- **网络**：能访问大模型 API（`api.openai.com` 等）和你的检测服务器

### 2.2 正式安装（pip）

```bash
pip install saitec-safe-guard-cli
safe-guard --help
```

### 2.3 源码安装（开发/调试）

```bash
git clone https://github.com/LiangRichard13/saitec-safe-guard-cli.git
cd saitec-safe-guard-cli

# 推荐：用 conda 创建独立环境，避免污染系统 Python
conda create -y -n saitec-guard python=3.12
conda activate saitec-guard

# 可编辑安装（含 dev 依赖）
pip install -e ".[dev]"

# 可选：含 mock detector 依赖（见 §9.1）
pip install -e ".[mock,dev]"
```

验证安装：

```bash
safe-guard --help
# 应输出 Usage 和 13 个命令列表
```

---

## 3. 快速上手（5 分钟）

跟着下面步骤跑通完整链路：**init → start → 发请求 → report → stop**。

### 步骤 1：初始化配置（指明要监控哪个端点）

```bash
safe-guard init --api-key "<你的X-API-Key>" \
    --detector-url "http://detector.example.com:8080" \
    --upstream "https://api.openai.com"
```

`--upstream` 是**你要监控的大模型端点**——可以是官方地址，也可以是任何 OpenAI / Anthropic 兼容端点：

| 场景 | --upstream 示例 |
|------|-----------------|
| OpenAI 官方 | `https://api.openai.com` |
| Anthropic 官方 | `https://api.anthropic.com` |
| DeepSeek 的 Anthropic 兼容口 | `https://api.deepseek.com/anthropic` |
| 中转站 / 网关 | `https://opencode.ai/zen/go/v1` |
| 本地部署模型 | `http://localhost:23333` |

`--endpoint-type`（协议格式）缺省时按 URL 自动猜测（含 `anthropic` → Anthropic Messages 格式，否则 → OpenAI Chat Completions 格式），输出会说明猜了什么；可显式指定 `openai-chat-completions` / `openai-responses` / `anthropic-messages`。

输出示例：

```
config_path: C:\Users\you\AppData\Local\saitec\config.json
detector_url: http://detector.example.com:8080
endpoint_type: openai-chat-completions（按 upstream URL 猜测，可用 --endpoint-type 显式指定）

服务映射（客户端 base_url → 本地端口 → 真实上游）:
  1. openai-chat-completions  [openai-chat-completions]  127.0.0.1:9001  →  https://api.openai.com
     客户端配置: OPENAI_BASE_URL=http://127.0.0.1:9001/v1

下一步:
  - 监控更多端点: safe-guard service add <name> --upstream <URL>
  - 启动服务:     safe-guard start
```

**要同时监控多个端点？** 用 `service add` 逐个加：

```bash
safe-guard service add deepseek-claude --upstream https://api.deepseek.com/anthropic
safe-guard service add local-llm --upstream http://localhost:23333 --port 9010
```

### 步骤 2：启动服务

```bash
safe-guard start
```

输出含服务映射块（客户端地址 → 本地端口 → 真实上游）和日志路径。

### 步骤 3：把客户端指到本地端口

按服务映射块的提示设置环境变量：

```bash
# OpenAI 兼容端点
export OPENAI_BASE_URL=http://127.0.0.1:9001/v1

# Anthropic 兼容端点
export ANTHROPIC_BASE_URL=http://127.0.0.1:9002
```

测试一下（透明转发，返回与直连完全一致）：

```bash
curl -X POST http://127.0.0.1:9001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"hello"}]}'
```

**注意**：请求大模型厂商的鉴权（`Authorization` / `x-api-key` 头）由客户端自带并原样透传——你需要自己配置好真实的模型 API key。

### 步骤 4：查记录

默认上报间隔 60 秒。等约 1 分钟后查询：

```bash
safe-guard report
```

如果想立即看到，可以临时调小上报间隔：

```bash
safe-guard config set detector.report_interval_sec 5
safe-guard restart
```

### 步骤 5：优雅停止

```bash
safe-guard stop
```

这会触发最后一次 flush + 上报，确保内存里的记录不丢。

---

## 4. 完整命令参考

> 13 个命令分 5 类。所有命令都支持 `--config <path>`（自定义配置文件）和 `--json`（机器可读 JSON 输出）。

### 4.1 配置类

#### `init` — 初始化配置

生成 `config.json`（**单服务**，监控多个端点用 `service add`）。**已存在时需 `--force` 才覆盖**。

```bash
safe-guard init --api-key "<KEY>" --detector-url "<URL>" --upstream "<监控端点>"
safe-guard init --api-key "<KEY>" --detector-url "<URL>" --upstream "<URL>" --force  # 覆盖现有

# 可选参数
#   --endpoint-type  协议格式（缺省按 URL 猜测）
#   --name           服务名（缺省用 endpoint_type）
#   --port           本地端口（默认 9001）
```

校验规则：
- `api_key` 必须 ≥ 8 字符（防止拼写错）
- `detector-url` / `upstream` 必须以 `http://` 或 `https://` 开头
- 非 TTY 且未给 `--upstream` → 报错（upstream 必须显式指定）

错误时退出码 1，stderr 中文错误信息。

#### `service` — 监控服务管理（子命令组）

```bash
# 列出所有监控服务（含客户端配置提示）
safe-guard service list

# 添加（--endpoint-type 缺省按 URL 猜测；--port 缺省从 9001 起自动分配空闲端口）
safe-guard service add <name> --upstream <URL> [--endpoint-type <T>] [--port <N>]
safe-guard service add deepseek-claude --upstream https://api.deepseek.com/anthropic
safe-guard service add local-llm --upstream http://localhost:23333 --port 9010

# 修改（至少一项）
safe-guard service set <name> [--upstream <URL>] [--port <N>] [--endpoint-type <T>] [--record-body/--no-record-body]

# 移除
safe-guard service remove <name>
```

**注意**：
- 所有修改写入 config.json 后需 `safe-guard restart` 生效
- `service add/set` 会自动检测 upstream 误配成完整端点 URL（如 `.../v1/chat/completions`）并**警告**（路径重复风险，不阻断）
- name 重名 / 不存在 → 报错 exit 1

#### `validate` — 校验配置

```bash
safe-guard validate
# 输出 "config valid" 或具体错误
```

#### `config` — 子命令组

```bash
# 读
safe-guard config get detector.url
safe-guard config get detector.api_key --json  # api_key 自动脱敏

# 写
safe-guard config set detector.report_interval_sec 30

# 删
safe-guard config unset log_level

# 全部列出
safe-guard config list
safe-guard config list --json
```

**注意**：
- `config set` 会自动备份当前 config 为 `config.json.bak.<时间戳>`
- `config set` 写入会**立即做一次完整校验**，校验失败则不写入（原子回滚）
- `api_key` 字段在 `get` / `list` 输出中**自动脱敏**为 `***`

### 4.2 生命周期类

#### `start` — 启动服务（后台）

```bash
safe-guard start                          # 默认配置
safe-guard start --report-interval 5      # 临时覆盖上报间隔（不写入 config）
safe-guard start --batch-size 100         # 临时覆盖批量大小
SAITEC_REPORT_INTERVAL=5 safe-guard start  # 等价：env 覆盖
```

`start` 会 fork 子进程跑代理端口。如果服务已在运行，会返回错误 `ALREADY_RUNNING` 提示用 `restart`。

**PID 文件** 写入 `safe-guard.pid`（在 config 所在目录）。

#### `stop` — 优雅停止

```bash
safe-guard stop
safe-guard stop --timeout 30  # 自定义超时（秒，默认 10）
```

Windows 实现：先写 `stop.flag` 让子进程优雅关闭（轮询检测），超时后 `taskkill /F` 兜底。
Unix 实现：`SIGTERM` → 超时后 `SIGKILL`。

#### `restart` — stop + start

```bash
safe-guard restart
```

适用场景：改了 config 后想让新配置生效。

#### `status` — 查看运行状态

```bash
safe-guard status        # 人类可读
safe-guard status --json # JSON
```

输出 `running`/`pid`/`services`/日志尾部。**注意**：`queue_depth` 字段当前未提供（外部读取无法获真实值）。

#### `logs` — 查看日志

```bash
safe-guard logs --tail 50        # 最后 50 行
safe-guard logs --follow         # 持续跟踪（Ctrl+C 退出）
safe-guard logs --service svc-a  # 按 service 过滤（简单子串匹配）
```

日志文件路径：`{config_dir}/logs/safe-guard.log`。**按日期自动切割**（每日午夜切出 `safe-guard.log.YYYY-MM-DD` 备份，运行期自动保留最近 14 天；手动清理用 `safe-guard purge`）。

### 4.3 运维类

#### `report` — 查询 SQLite 检测结果

```bash
safe-guard report                          # 最近 1 小时
safe-guard report --since "30m"            # 最近 30 分钟
safe-guard report --since "2h"             # 最近 2 小时
safe-guard report --since "7d"             # 最近 7 天
safe-guard report --since "2026-08-20T00:00:00Z"  # ISO8601
safe-guard report --service openai-chat-completions --limit 50
safe-guard report --json
```

#### `redo` — 手动重报某条记录（绕过游标）

```bash
safe-guard redo <record_id>
safe-guard redo <record_id> --json
```

适用场景：detector 改算法后想重新评估历史记录。`<record_id>` 是 UUID，从 `logs` 或 `report` 里查。

#### `purge` — 清理过期数据

```bash
safe-guard purge                    # 删 30 天前的 JSONL + 日志备份 + SQLite
safe-guard purge --retention-days 7 # 自定义保留期
safe-guard purge --dry-run          # 只看不动
```

清理三类：
- JSONL 记录文件（`records-*.jsonl`，按文件名日期）
- **日志切割备份**（`safe-guard.log.YYYY-MM-DD`，按文件名日期；活跃的 `safe-guard.log` 不会删）
- SQLite 中超期的检测记录

> 日志按天自动切割（午夜），服务运行期间自动保留最近 14 天；`purge` 用于手动/更彻底的清理。

### 4.4 调试类

#### `doctor` — 自检

```bash
safe-guard doctor               # 全量（含 API 探测）
safe-guard doctor --quick       # 只查本地（不调 detector）
safe-guard doctor --json
```

检查项：
- `config`：配置文件存在 + 解析
- `port:<N>`：每个 service 端口可用性
  - 服务运行时 → 端口应被监听（否则异常）
  - 服务未运行时 → 端口应可绑（被占则冲突）
- `disk`：数据目录所在盘 ≥ 1 GB 剩余
- `sqlite`：SQLite 完整性
- `jsonl`：JSONL 目录可写
- `api_key`：`detector.api_key` 已配置

输出每项 `status: ok / fail` + `detail`，最后 `all_ok: True/False`。

#### `tail` — 实时跟踪事件流

```bash
safe-guard tail                          # 所有 service
safe-guard tail --service svc-a          # 按 service 过滤
safe-guard tail --level error            # 按级别过滤（debug/info/warning/error）
```

类似 `tail -f`，读 JSONL 文件新写入的行。**注：仅适合交互式终端**，Agent 批量查询用 `report`。

---

## 5. 配置文件详解

### 5.1 文件位置

`config.json` 的查找顺序（先找到的优先）：

1. `--config <path>` 命令行参数
2. 环境变量 `SAITEC_CONFIG`
3. `platformdirs` 用户配置目录（默认）：
   - Windows: `%LOCALAPPDATA%\saitec\safe-guard\config.json`
   - macOS: `~/Library/Application Support/saitec/config.json`
   - Linux: `~/.config/saitec/config.json`

### 5.2 字段结构

`init` 后的初始配置（单服务）：

```json
{
  "config_version": 1,
  "detector": {
    "url": "http://detector.example.com:8080",
    "api_key": "your-api-key-here",
    "endpoint_path": "/detect",
    "report_interval_sec": 60,
    "batch_size": 500,
    "max_queue_size": 10000
  },
  "services": [
    {
      "name": "openai-chat-completions",
      "port": 9001,
      "upstream": "https://api.openai.com",
      "endpoint_type": "openai-chat-completions",
      "record_body": true
    }
  ],
  "log_level": "INFO"
}
```

`service add` 加多个端点后的 services 示例（DeepSeek Anthropic 口 + 本地模型）：

```json
"services": [
  {
    "name": "openai-chat-completions",
    "port": 9001,
    "upstream": "https://api.openai.com",
    "endpoint_type": "openai-chat-completions",
    "record_body": true
  },
  {
    "name": "deepseek-claude",
    "port": 9002,
    "upstream": "https://api.deepseek.com/anthropic",
    "endpoint_type": "anthropic-messages",
    "record_body": true
  },
  {
    "name": "local-llm",
    "port": 9003,
    "upstream": "http://localhost:23333",
    "endpoint_type": "openai-chat-completions",
    "record_body": true
  }
]
```

### 5.3 detector 段字段

| 字段 | 类型 | 含义 | 默认 |
|------|------|------|------|
| `url` | string | 检测服务器根 URL（只含 scheme+host+port） | — |
| `api_key` | string | `X-API-Key` 头值（脱敏显示） | — |
| `endpoint_path` | string | 上报 endpoint 路径（同一 IP:端口 下不同检测接口，如 `/api/v1/detect-v2`） | `/detect` |
| `report_interval_sec` | int | 上报周期（秒） | 60 |
| `batch_size` | int | 单次上报的批量大小 | 500 |
| `max_queue_size` | int | 内存队列上限（超出丢最旧） | 10000 |

> `url` 与 `endpoint_path` 分开配置的原因：避免 URL 带路径时的拼接歧义。上报完整地址 = `url.rstrip("/") + endpoint_path`。

### 5.4 services 段字段

| 字段 | 类型 | 含义 |
|------|------|------|
| `name` | string | service 标识，用于日志过滤（唯一） |
| `port` | int | 本地监听端口（0 = 自动分配） |
| `upstream` | string | 上游 base URL（**URL 前缀**，详见下方语义说明） |
| `endpoint_type` | string | 协议类型（决定 adapter 怎么解析记录） |
| `record_body` | bool | 是否记录请求/响应体（关掉则只记元数据） |

**支持的 endpoint_type**（v1）：
- `openai-chat-completions`（OpenAI `/v1/chat/completions`）
- `openai-responses`（OpenAI `/v1/responses`）
- `anthropic-messages`（Anthropic `/v1/messages`）

#### upstream 语义（重要）

upstream 是 **URL 前缀**，转发规则：

```
完整转发地址 = upstream + 客户端请求的原始路径
```

CLI 不自动加任何后缀——`/v1`、`/chat/completions` 这些路径是客户端 SDK 发请求时自带的，原样透传。所以 upstream 应配到"客户端请求路径之前"为止：

| 你的真实端点 | 客户端 SDK 发的路径 | 应配的 upstream |
|---|---|---|
| `https://api.openai.com/v1/chat/completions` | `/v1/chat/completions` | `https://api.openai.com` |
| `https://api.deepseek.com/anthropic/v1/messages` | `/v1/messages` | `https://api.deepseek.com/anthropic` |
| `https://opencode.ai/zen/go/v1/chat/completions` | `/chat/completions` | `https://opencode.ai/zen/go/v1` |
| `http://localhost:23333/v1/chat/completions` | `/v1/chat/completions` | `http://localhost:23333` |

**常见坑（路径重复）**：把完整端点 URL 配进 upstream（如 `http://localhost:23333/v1/chat/completions`），客户端再带一遍路径就变成 `.../v1/chat/completions/v1/chat/completions` → 404。`init` / `service add/set` / `config set` 检测到这种情况会**警告**（不阻断）。

### 5.5 配置优先级

CLI 参数 > 环境变量（`SAITEC_*`） > config.json。例如：

```bash
SAITEC_REPORT_INTERVAL=5 safe-guard start  # 覆盖 report_interval_sec
safe-guard start --report-interval 5        # 同上，但 CLI 优先级更高
```

常用 env var：
- `SAITEC_CONFIG` — 配置文件路径
- `SAITEC_API_KEY` — detector.api_key
- `SAITEC_DETECTOR_URL` — detector.url
- `SAITEC_ENDPOINT_PATH` — detector.endpoint_path
- `SAITEC_REPORT_INTERVAL` — detector.report_interval_sec
- `SAITEC_BATCH_SIZE` — detector.batch_size
- `SAITEC_LOG_LEVEL` — log_level

---

## 6. 集成到你的客户端

`safe-guard` 是**反向代理**，把它放在客户端和大模型 API 之间即可。

### 6.1 Claude Code

Claude Code 通过环境变量 `ANTHROPIC_BASE_URL` 指定 API 地址：

```bash
# 默认（直连 Anthropic）
# ANTHROPIC_BASE_URL=https://api.anthropic.com

# 改用 safe-guard 代理
ANTHROPIC_BASE_URL=http://127.0.0.1:9003 claude-code ...
```

配合 ccswitch 等工具可更灵活地切换（详见各自文档）。

### 6.2 Codex（OpenAI）

Codex 用 `OPENAI_BASE_URL`：

```bash
OPENAI_BASE_URL=http://127.0.0.1:9001 codex ...
```

### 6.3 自写 Python

```python
import openai

client = openai.OpenAI(
    base_url="http://127.0.0.1:9001/v1",  # 注意加 /v1
    api_key="...",  # 仍需要真实 key（safe-guard 不处理鉴权）
)

resp = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "hello"}],
)
```

### 6.4 curl 直接测

```bash
curl -X POST http://127.0.0.1:9001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"hi"}]}'
```

### 6.5 其他厂商 / 本地模型（OpenAI / Anthropic 兼容端点）

任何兼容端点都能监控——加一个 service，把客户端指到它的本地端口：

```bash
# DeepSeek 的 Anthropic 兼容口（Claude Code 走它）
safe-guard service add deepseek-claude --upstream https://api.deepseek.com/anthropic
#   → 本地 127.0.0.1:9002，ANTHROPIC_BASE_URL=http://127.0.0.1:9002

# 本地部署模型（LM Studio / Ollama / vLLM 等）
safe-guard service add local-llm --upstream http://localhost:23333 --port 9010
#   → 本地 127.0.0.1:9010，OPENAI_BASE_URL=http://127.0.0.1:9010/v1

# 中转站 / 网关
safe-guard service add zen --upstream https://opencode.ai/zen/go/v1 --port 9020

safe-guard restart
```

然后按 `service list` 输出的客户端配置提示设置对应环境变量即可。

---

## 7. 排错手册

### 7.1 `start` 报"已在运行"

```bash
# 看真实状态
safe-guard status

# 强制重启
safe-guard restart
```

如果 `status` 显示 `running: False` 但 `start` 还说"已在运行"，PID 文件残留（进程死了但文件没清）。手动清理：

```bash
# Windows
del "{config_dir}\safe-guard.pid"

# Unix
rm "{config_dir}/safe-guard.pid"
```

### 7.2 `report` 报"库不存在"

还没上报过数据。发请求等几个周期（默认 60 秒 × 1 = 60s）后再查。

### 7.3 `report` 显示空结果

几种原因：
1. **时间窗口不对**：`--since` 默认 1 小时，请求时间早于此。试试 `--since 7d`
2. **service 名不对**：`--service` 必须精确匹配。看 `config list` 里的 `services.<name>`
3. **上报失败了**：看 `logs` 里有没有 `auth failed` / `server error`

### 7.4 检测服务器返回 401 / 403

**症状**：`logs` 里出现：

```
ERROR saitec.runtime.runtime: X-API-Key 失效，停止上报：auth failed (401) at http://detector:8080/detect; 检查 detector.api_key 是否与检测服务器一致，需要重设请用 `safe-guard init --api-key ... --detector-url ... --force`
```

**修复**：重设 api_key：

```bash
safe-guard init --api-key "NEW_KEY" --detector-url "http://detector:8080" --force
safe-guard restart
```

### 7.5 端口被占用

`doctor` 会显示 `port:<N> status:fail detail: ... 已被其它进程占用`。

**修复**：改 config 里的 `services[*].port`，或关掉占用进程。

### 7.6 上游不可达（502）

如果 upstream `https://api.openai.com` 不可达，代理会返回 502 + 记录 error。看 `report` / `logs`：

```
"status_code": 502,
"error": "upstream error: ..."
```

`safe-guard` 不会重试上游——客户端需自己重试。

### 7.7 Windows 中文乱码

如果 `safe-guard --help` 输出是 `????`，可能是控制台代码页问题。`safe-guard` 在 pipe 模式下会自动强制 UTF-8，但 tty 下跟随系统。

**修复**：
- 用 Windows Terminal / VS Code 集成终端（默认 UTF-8）
- 或 `chcp 65001` 切换当前 cmd 代码页到 UTF-8

### 7.8 SQLite 文件损坏

`doctor` 报 `sqlite status:fail`。**不要手动删 results.db**（会丢历史）。

最可能原因：磁盘满 / 权限错。先看磁盘：

```bash
safe-guard doctor
# 看 disk 项
```

如果磁盘正常但 SQLite 仍坏，把 `results.db` 备份后重命名让 safe-guard 重建（**会丢历史**）：

```bash
mv "{config_dir}/results.db" "{config_dir}/results.db.corrupt"
safe-guard restart
# 重新发请求触发上报，会自动建新 results.db
```

---

## 8. 安全注意事项

### 8.1 api_key 明文存储

`config.json` 里 `detector.api_key` 是**明文**（仅在 CLI 输出中隐藏）。

- **Linux/macOS**：`init` 时会自动 `chmod 600`（仅当前用户可读写）
- **Windows**：自动限制做不到（需管理员），会提示 warning：
  ```bash
  icacls "{config_dir}\config.json" /inheritance:r /grant:r "%USERNAME%:(R,W)"
  ```

### 8.2 api_key 注入方式

**推荐**：用 `--api-key` 参数（不入 shell history）或 `SAITEC_API_KEY` 环境变量。

**避免**：
- 把 api_key 写在脚本里被 git 提交
- 把 api_key 通过 `--api-key` 写在命令行（部分 shell 会记 history）

### 8.3 日志可能含敏感内容

`logs` / `tail` 输出含**完整请求/响应体**，可能含 prompt 内容（个人隐私 / 公司机密）。**禁止**：
- 把日志文件 commit 到 git
- 把日志文件分享到公网
- 让无关用户访问 config 目录

如果 `record_body: false`，日志/JSONL 不含 body，但元数据（timestamp、model、status_code 等）仍记录。

### 8.4 权限分离

如果多人共用一台机器：
- `config.json` 必须 `chmod 600` / Windows 限制到当前用户
- `records/`、`results.db`、`logs/` 同理（init 时已自动设权限）

否则其他用户可读你的请求历史 / 检测结果。

---

## 9. 进阶用法

### 9.1 用本地 mock detector 联调

如果你还没有真 detector，可以用内置 mock 模拟（**仅开发/联调**）。

**启动 mock**：

```bash
# 安装 mock 依赖
pip install "saitec-safe-guard-cli[mock]"

# random 模式（默认）：按 5% 概率随机标 violation
uvicorn server:app --app-dir tests/mock_detector --host 127.0.0.1 --port 8000

# llm 模式：真实大模型判断内容是否安全（在 tests/mock_detector/.env 配 key）
MOCK_DETECTION_MODE=llm uvicorn server:app --app-dir tests/mock_detector --host 127.0.0.1 --port 8000
```

mock 提供：
- `POST /detect` — 接收上报，`random` 模式按概率返回、`llm` 模式逐条调模型判定（失败降级为 `error` 结论，不阻断）
- `GET /records` — 查询已处理记录（支持 `service` / `risk` 过滤）
- `GET /health` — 健康检查（含当前检测模式）
- `X-API-Key` 期望值默认为 `mock-test-key`（env `MOCK_DETECTION_API_KEY` 改）

详细配置（llm 模式端点/模型/超时等）见 `tests/mock_detector/README.md`。

### 9.2 自定义上报间隔调试

临时调小上报间隔（不写 config）：

```bash
safe-guard start --report-interval 5  # 5 秒一次
# 或
SAITEC_REPORT_INTERVAL=5 safe-guard start
```

发请求后 5 秒即可在 `report` 中查到。

### 9.3 多 service 端口冲突排查

如果有端口被占用，启动会失败。诊断：

```bash
# 看哪个端口被占
netstat -ano | grep ":9001" | grep LISTENING

# Windows 杀进程
taskkill /F /PID <pid>

# Unix
kill -9 <pid>
```

### 9.4 查看原始 JSONL

```bash
# 今天的记录
cat {config_dir}/records/records-$(date +%Y-%m-%d).jsonl | head -3 | python -m json.tool
```

JSONL 每行一个归一化 Record，包含完整 request/response body。

### 9.5 重报某条记录

```bash
safe-guard redo <record_id>
```

适用场景：detector 规则更新后想重新评估历史。

---

## 附录 A：退出码

| 退出码 | 含义 |
|--------|------|
| 0 | 成功 |
| 1 | 用户错误（参数错、校验失败） |
| 2 | 运行时错误（启动失败、配置未找到、IO 失败） |
| 3 | 检测服务器错误（AUTH 失败、长时间不可达） |
| 4 | 内部错误（异常未捕获、Python bug） |

`--json` 输出对应有 `ok: true/false` + `error.code` 字段。

---

## 附录 B：与 Claude Code / Codex 等的协作

`safe-guard` 本身**支持 Agent 操作**——所有命令都有 `--json` 输出，错误信息结构化。

**典型 Agent 调用流程**：

```bash
# 1. 检查是否在跑
safe-guard status --json

# 2. 如果不在跑
safe-guard init --api-key "$KEY" --detector-url "$URL" --force
safe-guard start --json

# 3. 健康检查
safe-guard doctor --json

# 4. 查最近结果
safe-guard report --since "1h" --json
```

返回的 JSON 可直接被 Claude / Codex 解析做后续决策。

---

## 附录 C：相关文档

- `docs/design/saitec-safe-guard-cli-design.md` — 总体设计
- `docs/design/architecture.md` — 6 层架构
- `docs/design/data-model.md` — SQLite + JSONL 数据模型
- `docs/integration/detector-api.md` — **检测服务器对接文档**（给服务端接口开发人员）
- `docs/issues/cli-usage-issues.md` — 已知问题与排错历史

---

## 反馈

发现 bug / 缺功能 → 提 Issue：https://github.com/LiangRichard13/saitec-safe-guard-cli/issues
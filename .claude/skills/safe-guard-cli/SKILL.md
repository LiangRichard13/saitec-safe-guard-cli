---
name: safe-guard-cli
description: 操作 saitec safe-guard CLI（safe-guard 命令）——大模型 API 流量的反向代理监控工具。当用户提到 safe-guard、监控 LLM API 调用、把 Claude Code/Codex/脚本的请求代理到本地端口、上报流量到安全检测服务器、查检测结果（violation/clean）、或要求配置/启停/排错这个工具时，使用本 skill。含初始化、服务增删改、启停、检测查询、定时心跳监控、排错的完整操作方法。
---

# safe-guard CLI 操作指南（Agent 版）

反向代理监控工具：客户端把大模型 API 请求指向本地端口，safe-guard 透明转发到真实上游，同时记录请求/响应并周期上报到检测服务器，检测结果存本地 SQLite。

```
客户端 ──→ 127.0.0.1:<port> ──→ upstream（真实端点）
              │ 记录 Record
              ▼
           JSONL 落盘 ──周期上报──→ detector（检测服务器）
              ▼
           results.db（SQLite）←── safe-guard report 查询
```

## 三个核心概念

1. **service**：一个监控端点 = `{name, port, upstream, endpoint_type}`。可配任意多个（官方/厂商兼容口/本地模型均可）。
2. **upstream 是 URL 前缀**：完整转发地址 = `upstream + 客户端请求路径`。CLI 不改写路径。配成完整端点 URL（如 `.../v1/chat/completions`）会导致路径重复 404——CLI 会警告但不阻止。
3. **detector**：检测服务器，`POST {detector.url}{detector.endpoint_path}`（默认 `/detect`），`X-API-Key` 鉴权。401/403 → CLI **停止上报**直到用户重配。

## Agent 操作契约（重要）

- **一律加 `--json`**：输出为稳定 JSON（`{"ok": bool, "data": ..., "error": {code, message}}`），从 stdout 解析；错误信息走 stderr。
- **退出码**：`0` 命令执行成功 · `1` 用户错误（参数/校验）· `2` 运行时错误（未运行/端口占用/不存在）· `3` 检测器错误 · `4` 内部错误。
  ⚠️ exit 0 ≠ 服务健康：`status` 在服务未运行时也返回 exit 0（`ok:true, running:false`）——**健康判断必须查 `data.running` 字段**，不能只看退出码。
- **非交互**：`init` 必须给全 `--api-key`（≥8 字符）`--detector-url` `--upstream` 三个参数，TTY 交互在 Agent 环境不可用。
- **配置路径**：默认 platformdirs 用户目录（Windows 为 `%LOCALAPPDATA%\saitec\config.json`）；用环境变量 `SAITEC_CONFIG=/path/config.json` 隔离实例（测试/多套配置必备）。数据（records/logs/results.db）跟随 config 所在目录。
- **改配置不热生效**：`config set` / `service add/set/remove` 后需 `restart`。
- **Windows Git Bash 陷阱**：以 `/` 开头的参数（如 `/detect`、`/api/v1/...`）会被 MSYS 转成 `C:/Program Files/Git/...`。命令前加 `MSYS_NO_PATHCONV=1`。

## 命令速查

```bash
# 生命周期
safe-guard init --api-key KEY --detector-url URL --upstream URL [--endpoint-type T] [--name N] [--port P] [--force]
safe-guard start [--report-interval N] [--batch-size N]   # 后台子进程 + PID 文件
safe-guard stop [--timeout N]
safe-guard restart
safe-guard status --json        # running/pid/services/最近日志
safe-guard doctor --json        # 自检（config/端口/磁盘/SQLite/JSONL/api_key）

# 监控服务管理
safe-guard service list --json
safe-guard service add NAME --upstream URL [--endpoint-type T] [--port N]
safe-guard service set NAME [--upstream URL] [--port N] [--endpoint-type T] [--record-body B]
safe-guard service remove NAME

# 配置
safe-guard config get KEY [--json]          # api_key 自动脱敏
safe-guard config set KEY VALUE [--json]    # 原子写入：校验失败不落盘，自动备份
safe-guard config unset KEY
safe-guard config list --json
safe-guard validate

# 检测结果与运维
safe-guard report [--since 1h|30m|7d|ISO8601] [--service NAME] [--limit N] --json
safe-guard redo RECORD_ID --json            # 重报单条（绕过游标）
safe-guard purge [--retention-days N] [--dry-run]   # 清 JSONL/日志备份/SQLite
safe-guard logs --tail N [--service NAME]
```

`--endpoint-type` 枚举：`openai-chat-completions` / `openai-responses` / `anthropic-messages`。缺省按 upstream URL 猜测（含 `anthropic` → anthropic-messages，否则 openai-chat-completions），输出里有 `endpoint_type_guessed` 标记。

## 常见任务

### 1. 从零启动监控一个端点

```bash
export SAITEC_CONFIG=$WORK_DIR/config.json
safe-guard init --api-key "$DETECTOR_KEY" --detector-url "$DETECTOR_URL" \
    --upstream "https://api.deepseek.com/anthropic" --json
safe-guard start --json
```

start 输出的 `data.client_hint` 给出各服务客户端应配的 base_url（如 `ANTHROPIC_BASE_URL=http://127.0.0.1:9001`）。

可选验证（真实流量走一遍代理）：`curl -s -X POST http://127.0.0.1:<port>/v1/chat/completions -H "Content-Type: application/json" -d '{"model":"x","messages":[]}'`——上游不通也返回 502/401，但记录已生成，等一个上报周期后 `report --json` 可见（证明记录-上报链路通）。

### 2. 加 / 改 / 删监控端点

```bash
safe-guard service add local-llm --upstream http://localhost:23333 --port 9010 --json
safe-guard service set local-llm --upstream http://localhost:11434 --json
safe-guard service remove local-llm --json
safe-guard restart --json
```

`add` 不给 `--port` 时自动从 9001 起分配空闲端口；name 重复报 `NAME_EXISTS`。

### 3. 查检测结果（violation 优先）

```bash
safe-guard report --since 1h --json     # data.results[]: record_id/detection_status/risk_level/model/detail
safe-guard report --since 7d --limit 500 --json | jq '[.data.results[] | select(.detection_status=="violation")]'
```

`detection_status`：`clean` / `suspicious` / `violation` / `error`。`error` 表示检测服务器自身失败（detail.reason 有原因），不是流量违规。

### 4. 心跳定时监控汇报

单次检查（适合被宿主调度器/cron/Monitor 周期调用）：

```bash
safe-guard status --json && safe-guard report --since 10m --limit 500 --json
```

要点：
- **查 `data.running` 字段**判断存活（exit 0 不代表在跑）
- **`--limit 500`**：report 默认 100 会截断高流量窗口，监控场景必须显式调大
- 汇报优先级：服务掉线 > violation（含 `detail.reason`）> `detection_status=error`（detector 自身故障）
- **盲区兜底**：detector 401 后 CLI 故意停摆，此时 `running=true` 且 report 无 error——健康巡检时可加 `safe-guard logs --tail 50` 检查有无 `X-API-Key 失效`
- 周期建议 ≥ detector 的 `report_interval_sec`（默认 60s），否则看到的总是旧数据；持久的宿主级定时任务用 Claude Code 的 cron/Monitor 机制做，本 CLI 自身不提供调度

### 5. 排错决策树

1. `safe-guard doctor --json` —— `port:*` fail = 端口冲突；`config` fail = 配置坏
2. `status` 未运行但怀疑有残留 —— 直接 `start`：CLI 对死 PID 自动覆盖（stop 也自带 STALE_PID 自愈清理）。真正报 `ALREADY_RUNNING` 说明 PID 确实活着，是另一个实例在跑（可能用了不同 SAITEC_CONFIG），先找到它再决定停谁
3. 日志出现 `X-API-Key 失效` → detector 的 api_key 不匹配，重 `init --force` 或 `config set detector.api_key`
4. `report` 空结果 → 时间窗口不对（扩大 `--since`）或上报周期未到（等 60s）
5. 更细排错（SQLite 损坏/GBK 乱码/续传机制）见 [references/operations.md](references/operations.md)

## 关键陷阱汇总

| 陷阱 | 规避 |
|------|------|
| upstream 配成完整端点 URL → 路径重复 404 | upstream 配到"客户端请求路径之前"为止（如 `http://localhost:23333` 而非 `.../v1/chat/completions`） |
| Git Bash 把 `/detect` 等参数转成 Windows 路径 | 命令前缀 `MSYS_NO_PATHCONV=1` |
| detector 401 后上报停摆 | 日志会写明；重配 key 后 `restart` 恢复 |
| 改配置没生效 | `restart` |
| 监控需要真实模型 key | safe-guard 不做鉴权，`Authorization` 头由客户端自带透传；测试用 mock（见 references） |

## 深入阅读

- [references/operations.md](references/operations.md) —— 排错手册、mock detector 联调、redo/purge 细节、detector 对接契约摘要
- 项目内文档：`docs/user-guide.md`（人类视角手册）、`docs/integration/detector-api.md`（服务端对接契约）

# 数据模型设计

- **日期**：2026-08-14
- **状态**：待评审（Draft）
- **配套文档**：`saitec-safe-guard-cli-design.md`（总体设计）、`architecture.md`（架构分层）

## 1. 文档目的

定义 saitec-safe-guard-cli 的**两层持久化**（SQLite 检测结果库 + JSONL 原始记录落盘）的字段、表结构、索引、协作关系与演进策略。**不写实现，只定义形态。**

## 2. 持久化分层

```
┌─────────────────────────────────────────────────────────────────────┐
│  JSONL 记录落盘（recorder）                                          │
│  - 全部原始记录：请求 / 响应 / 上游耗时 / 错误                        │
│  - 落盘即"已采集"，未上报也可保留                                    │
│  - 文件按天分片：records-YYYY-MM-DD.jsonl                            │
└───────────────────────────────────┬─────────────────────────────────┘
                                    │ record_id 关联
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│  SQLite 检测结果库（store）                                          │
│  - 已上报 + 已收到检测结论的记录                                      │
│  - 用于 status / report 查询                                        │
│  - 用 report_cursor 支持断点续传                                     │
└─────────────────────────────────────────────────────────────────────┘
```

**关键原则**：

- **JSONL 是源、SQLite 是果**。任何"已采集但未上报"的记录只存在于 JSONL。
- **idempotent**：以 `record_id`（UUID）为唯一键，重复上报安全。
- **可丢弃**：SQLite 数据可由 JSONL 重建（如果需要），但反向不行——这是单向的"已确认状态"。

## 3. SQLite 设计

### 3.1 主表 `detection_results`

#### 字段定义

| 字段 | 类型 | 是否可空 | 默认 | 说明 |
|---|---|---|---|---|
| `id` | INTEGER | NOT NULL | autoincrement | 主键，自增 |
| `record_id` | TEXT (UUID) | NOT NULL | — | 与 JSONL 内 `record_id` 唯一对应。唯一索引。 |
| `service` | TEXT | NOT NULL | — | 服务名（来自配置，如 `openai-chat-completions`） |
| `endpoint_type` | TEXT | NOT NULL | — | 端点类型枚举 |
| `upstream` | TEXT | NOT NULL | — | 上游 URL |
| `timestamp` | TEXT (ISO8601) | NOT NULL | — | 请求发生时间（来自 `Record.timestamp`） |
| `detected_at` | TEXT (ISO8601) | NOT NULL | — | 检测侧返回结论的时间 |
| `model` | TEXT | NULL | — | 模型名（如 `gpt-4o`），来自 `request.model` |
| `request_excerpt` | TEXT | NULL | — | 请求摘要（首 N 字符 + "..."，可配置） |
| `response_excerpt` | TEXT | NULL | — | 响应摘要（重组后的完整 content，可被脱敏） |
| `prompt_tokens` | INTEGER | NULL | — | 来自 `response.usage.prompt_tokens` |
| `completion_tokens` | INTEGER | NULL | — | 来自 `response.usage.completion_tokens` |
| `status_code` | INTEGER | NOT NULL | — | 上游响应状态码 |
| `latency_ms` | INTEGER | NOT NULL | — | 端到端延迟 |
| `finish_reason` | TEXT | NULL | — | stop / length / error / 等 |
| `detection_status` | TEXT | NOT NULL | — | `clean` / `suspicious` / `violation` / `error` |
| `risk_level` | TEXT | NULL | — | `low` / `medium` / `high` / `critical` |
| `detection_detail` | TEXT (JSON) | NULL | — | 检测侧返回的完整结论（原始 JSON 文本） |
| `error` | TEXT | NULL | — | 上报失败或上游错误时的错误描述 |

#### 索引

| 索引名 | 字段 | 用途 |
|---|---|---|
| `pk_id` | `id` | 主键 |
| `uq_record_id` | `record_id` UNIQUE | 幂等性 + 与 JSONL 关联 |
| `idx_timestamp` | `timestamp` | 按时间范围查询 |
| `idx_service_timestamp` | `service`, `timestamp` | `report --service xxx --since ...` |
| `idx_detection_status` | `detection_status`, `timestamp` | 按风险状态筛 |
| `idx_risk_level` | `risk_level`, `timestamp` | 按风险等级筛 |

#### 约束

- `detection_status` 取枚举：`clean` / `suspicious` / `violation` / `error`
- `risk_level` 取枚举：`low` / `medium` / `high` / `critical`，或 NULL（未评级）
- `record_id` 唯一（防止重复上报）

### 3.2 辅助表 `report_cursor`

只用一行：维护"已上报到的位置"，支持**断点续传**。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER | 固定为 1（单行表） |
| `last_record_id` | TEXT | 上次成功上报的最后一个 `record_id` |
| `last_timestamp` | TEXT (ISO8601) | 上次成功上报的最后一个 `timestamp` |
| `updated_at` | TEXT (ISO8601) | 游标更新时间 |

**为什么需要**：在上报失败（检测服务器不可达）时，工具需要在重启后能从 `last_record_id` 之后继续，避免从头扫描 JSONL。

**算法**：

```python
# 启动时
cursor = store.get_cursor()   # 读 (last_record_id, last_timestamp)
for record in recorder.since(last_record_id):
    results = await reporter.report([record])
    await store.save_results(results)
    cursor.advance(record.record_id, record.timestamp)
```

### 3.3 建表 SQL（参考）

```sql
CREATE TABLE detection_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id TEXT NOT NULL UNIQUE,
    service TEXT NOT NULL,
    endpoint_type TEXT NOT NULL,
    upstream TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    model TEXT,
    request_excerpt TEXT,
    response_excerpt TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    status_code INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL,
    finish_reason TEXT,
    detection_status TEXT NOT NULL CHECK (detection_status IN ('clean', 'suspicious', 'violation', 'error')),
    risk_level TEXT CHECK (risk_level IN ('low', 'medium', 'high', 'critical')),
    detection_detail TEXT,
    error TEXT
);

CREATE INDEX idx_timestamp ON detection_results (timestamp);
CREATE INDEX idx_service_timestamp ON detection_results (service, timestamp);
CREATE INDEX idx_detection_status ON detection_results (detection_status, timestamp);
CREATE INDEX idx_risk_level ON detection_results (risk_level, timestamp);

CREATE TABLE report_cursor (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_record_id TEXT,
    last_timestamp TEXT,
    updated_at TEXT NOT NULL
);

INSERT INTO report_cursor (id, last_record_id, last_timestamp, updated_at)
VALUES (1, NULL, NULL, '1970-01-01T00:00:00Z');
```

## 4. JSONL 记录落盘格式

### 4.1 文件组织

- 文件命名：`records-YYYY-MM-DD.jsonl`（按天分片）
- 路径：`<config_dir>/records/records-YYYY-MM-DD.jsonl`
- 写入方式：append-only，每行一个 JSON 对象，UTF-8 编码
- 顺序追加（用 `O_APPEND` 标志确保原子性）

### 4.2 单行格式（与 `core.models.Record` 一致）

```json
{
  "record_id": "550e8400-e29b-41d4-a716-446655440000",
  "service": "openai-chat-completions",
  "endpoint_type": "openai-chat-completions",
  "upstream": "https://api.openai.com",
  "path": "/v1/chat/completions",
  "timestamp": "2026-08-14T12:00:00.123Z",
  "elapsed_ms": 812,
  "status_code": 200,
  "error": null,
  "request": {
    "model": "gpt-4o",
    "messages": [
      {"role": "system", "content": "You are a helpful assistant."},
      {"role": "user", "content": "Hello!"}
    ],
    "tools": []
  },
  "response": {
    "content": "Hi! How can I help you today?",
    "finish_reason": "stop",
    "usage": {"prompt_tokens": 12, "completion_tokens": 9},
    "raw": null
  }
}
```

**字段约束**：

- `record_id`（UUID v4）由 `proxy` 生成，**保证唯一**
- `timestamp` 毫秒精度 ISO8601（带时区）
- `request.raw` / `response.raw` 受 `record_body` 开关控制：关时为 `null`
- `error` 非空时表示该请求失败（上游超时 / 5xx 等），其余字段仍尽量填充

### 4.3 落盘时机

`proxy` 完成后调用 `recorder.enqueue(record)`：

- 同步：写入内存队列
- 异步：后台任务按 batch / timer 追加到 JSONL

**关键不变量**：JSONL 写入成功才视作"已采集"，否则重试。

### 4.4 读取与重放（仅为崩溃恢复 / 启动续传，不用于正常上报）

```python
# 从某个游标位置开始读取（仅用于：进程崩溃后重启、磁盘故障恢复）
# 正常运行时的上报靠 runtime 调 recorder.flush()，**不读 JSONL**。
def iter_records_since(cursor: ReportCursor) -> Iterator[Record]:
    for file in sorted_records_files(since=cursor.last_timestamp):
        with open(file) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    log_error(f"损坏的 JSONL 行已跳过: {file}:{line_no}")
                    continue
                # ⚠️ 联合 (timestamp, record_id) 比较：UUID v4 字典序 ≠ 时间序
                if (rec["timestamp"], rec["record_id"]) > (cursor.last_timestamp, cursor.last_record_id):
                    yield rec
```

**关键点**：

- 即使单行 JSONL 损坏，**只跳过该行**，继续读下一行（不中断续传）。
- 时间序判据用 `(timestamp, record_id)` 联合比较，**严格大于游标才回放**。
- 正常上报路径不走 JSONL；JSONL 仅在**启动续传**和**崩溃恢复**时被读取。

## 5. SQLite 与 JSONL 协作

### 5.1 写路径

```
proxy
  → recorder.enqueue(record)             [push 到内存队列]
  → 每隔 report_interval_sec：
    recorder.flush() → batch              [从**内存队列**取批]
    reporter.report(batch) → results      [HTTP 上报]
    store.save_results(results)           [SQLite 写入]
    store.advance_cursor(last)            [SQLite 更新游标]
```

> 正常上报路径**不读 JSONL**。JSONL 仅在启动续传 / 崩溃恢复时由 `iter_records_since(cursor)` 回放。

### 5.2 读路径

```
cli report --service xxx --since 1h
  → store.query(...)                      [SQLite 读取]
```

**注意**：report 命令只查 SQLite（**已确认的检测结果**），不查 JSONL（**尚未上报的**）。如果要看"未上报但已采集"的，需要一个独立的命令（未来 v2 项）。

### 5.3 故障恢复

| 场景 | 行为 |
|---|---|
| 进程崩溃，JSONL 已写、上报未完成 | 重启后从 `last_record_id` 之后继续上报。不丢数据。 |
| JSONL 没写、进程崩溃 | 该次记录丢失（不可避免——但极少见，因为 recorder 入队 + 落盘速度极快） |
| SQLite 损坏 | 用 JSONL 重建（写一个 `tools/rebuild_sqlite.py` 脚本，作为运维工具） |
| 检测服务器更换 schema | `detection_detail` JSON 字段保留完整原始响应，前端可兼容解析 |

## 6. 字段对应关系（数据流）

```
Record (core)                  JSONL                           SQLite (detection_results)
─────────────────────────      ─────────────────────────       ─────────────────────────────
record_id                  →   record_id                   →   record_id (UNIQUE)
service                    →   service                     →   service
endpoint_type              →   endpoint_type               →   endpoint_type
upstream                   →   upstream                    →   upstream
timestamp                  →   timestamp                   →   timestamp
                                                                 + detected_at (新增)
model                      →   request.model               →   model
                                (request.content 取首 N)  →   request_excerpt
                                (response.content 完整)   →   response_excerpt
                                                             （脱敏后落库）
prompt_tokens              →   response.usage.prompt_tokens → prompt_tokens
completion_tokens          →   response.usage.completion_tokens → completion_tokens
status_code                →   status_code                 →   status_code
elapsed_ms                 →   elapsed_ms                  →   latency_ms
                                                                 (字段名重命名：Record 端用 elapsed_ms 强调端到端，SQLite 用 latency_ms 反映传统时延列命名约定，不影响语义)
finish_reason              →   response.finish_reason      →   finish_reason
error                      →   error                       →   error
                                                           →   detection_status (新增)
                                                           →   risk_level (新增)
                                                           →   detection_detail (新增，来自检测响应)
```

**JSONL 直接由 `Record` 序列化；SQLite 多出 `detection_*` 字段（来自检测响应）。**

## 7. 脱敏策略

数据库记录要避免成为新的泄密源：

- `response_excerpt`：默认长度上限（200 字符），工具调用 `core.redact_headers` 脱敏模式对内容做基本扫描（email / API key 模式）
- `request_excerpt`：同样限制长度 + 脱敏
- `detection_detail`：保留原始 JSON，但**按字段深度限制**（超过 5 层折叠）
- `request.messages` / `response.content` 完整内容 → **不进 SQLite**，仅在 JSONL 中存在（受 `record_body` 开关控制）

## 8. 查询范式

### 8.1 `status` 命令（运行时）

不查 SQLite，**直接查 `runtime` 的内存状态**（每个 service 的状态、队列深度、上报时间）。

### 8.2 `report` 命令（事后查询）

查 SQLite 的 `detection_results` 表：

```sql
-- 默认：最近 1h 所有结果
SELECT * FROM detection_results
WHERE timestamp > datetime('now', '-1 hour')
ORDER BY timestamp DESC
LIMIT 100;

-- 按服务
SELECT * FROM detection_results
WHERE service = ? AND timestamp > ?
ORDER BY timestamp DESC;

-- 按风险等级
SELECT * FROM detection_results
WHERE risk_level IN ('high', 'critical')
  AND timestamp > ?
ORDER BY timestamp DESC;
```

**`--json` 形态输出**：直接序列化为 JSON 数组，便于 Agent 解析。

## 9. 演进策略

| 演进 | 是否需要迁移 | 做法 |
|---|---|---|
| 增加新字段（带默认值） | 否 | ALTER TABLE ADD COLUMN with DEFAULT |
| 删字段 | 是 | 备份 + 迁移脚本 |
| 改索引 | 是 | DROP INDEX + CREATE INDEX |
| 改 `detection_status` 枚举 | 是 | 写迁移脚本 |
| 升级 SQLite schema 版本 | 是 | 引入 `schema_version` 表 + 启动时检查 |

**`schema_version` 表**（演进支持）：

```sql
CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
```

启动时检查当前 schema 版本，按需执行迁移。

## 10. 不放进数据库的东西

明确边界，避免数据库变成"杂物间"：

- ❌ **配置**（`config.json` 直接放文件系统）
- ❌ **日志**（`stderr` 输出 / 滚动日志文件）
- ❌ **临时缓存**（运行时内存）
- ❌ **完整请求/响应 body**（仅留 excerpt；完整 body 只在 JSONL）

## 11. 与其他文档的对应

| 总体设计 | 本文档 |
|---|---|
| §6 归一化记录 schema | §4.2 JSONL 行格式 |
| §3 检测结果存储 = SQLite | §3 SQLite 设计 |
| §9 错误处理 | §5.3 故障恢复 |
| §10 敏感信息脱敏 | §7 脱敏策略 |
| architecture.md §4 Layer 2 `store` | §3 SQLite 表 + §4 JSONL 协作 |
| architecture.md §4 Layer 2 `recorder` | §4 JSONL 落盘 |

# 检测服务器对接文档（Detector API）

> 面向**安全检测服务器的接口开发人员**：`safe-guard` CLI 会按本文档向你开发的服务上报归一化记录，并期望按本文档的格式返回检测结果。请对照实现。

---

## 目录

1. [总览](#1-总览)
2. [端点与鉴权](#2-端点与鉴权)
3. [请求格式](#3-请求格式)
4. [响应格式](#4-响应格式)
5. [状态码与重试语义](#5-状态码与重试语义)
6. [幂等性](#6-幂等性)
7. [流量特征](#7-流量特征)
8. [最小实现参考](#8-最小实现参考)
9. [常见问题](#9-常见问题)

---

## 1. 总览

```
┌────────────┐   周期批量上报    ┌──────────────┐
│ safe-guard │ ───────────────→ │  检测服务器   │
│  (客户端)  │ ←─────────────── │  (你们实现)   │
└────────────┘   返回检测结果    └──────────────┘
```

- `safe-guard` 在用户本机做反向代理，把大模型 API 调用归一化为 Record
- 每个上报周期（默认 60 秒）把**一批** Record `POST` 到检测服务器
- 检测服务器对每条 Record 返回检测结论（clean / violation / 风险等级）
- safe-guard 把结论写入本地 SQLite 供用户查询（`safe-guard report`）

**你们只需要实现一个 HTTP 端点**（默认 `POST /detect`）。

---

## 2. 端点与鉴权

### 2.1 URL

```
POST {base_url}{endpoint_path}
```

| 部分 | 谁决定 | 默认值 | 说明 |
|------|--------|--------|------|
| `base_url` | 用户配置 `detector.url` | 无（必配） | 形如 `http://10.0.1.5:8080`，只含 scheme + host + port |
| `endpoint_path` | 用户配置 `detector.endpoint_path` | `/detect` | 支持同一 IP:端口 下的不同检测接口（如 `/api/v1/detect-v2`） |

配置方式（用户侧，供你们向用户说明）：

```bash
safe-guard init --api-key "<KEY>" --detector-url "http://10.0.1.5:8080"
# 或进阶：
safe-guard config set detector.endpoint_path /api/v1/detect-v2
```

### 2.2 请求头

| Header | 必填 | 说明 |
|--------|------|------|
| `X-API-Key` | ✅ | 用户在 safe-guard 侧配置的 api_key。服务端校验不匹配返回 401 |
| `Content-Type` | ✅ | `application/json`（由 HTTP 客户端自动设置） |

**鉴权失败的处理**：返回 `401` 或 `403`。safe-guard 收到后会**停止上报**并提示用户重新配置（不会无限重试）。建议响应体带简短原因（会进入用户日志辅助排查）。

---

## 3. 请求格式

### 3.1 顶层结构

```json
{
  "batch": [
    { "record_id": "...", "...": "（一条 Record，见 3.2）" },
    { "record_id": "...", "...": "..." }
  ]
}
```

- `batch` 是数组，批量大小默认最多 **500 条**（用户可配 `detector.batch_size`）
- 批次内按时间顺序排列

### 3.2 Record 字段（batch 内每条）

```json
{
  "record_id": "e0dd6216-8518-4634-a5e0-f97fd7672c52",
  "service": "openai-chat-completions",
  "endpoint_type": "openai-chat-completions",
  "upstream": "https://api.openai.com",
  "path": "/v1/chat/completions",
  "timestamp": "2026-08-21T01:45:01.675+00:00",
  "elapsed_ms": 2,
  "status_code": 200,
  "error": null,
  "request": { "model": "gpt-4o", "messages": ["..."], "tools": null, "stream": false },
  "response": { "content": "Hello world", "finish_reason": "stop", "usage": { "prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7 }, "raw": null }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `record_id` | string (UUIDv4) | **全局唯一**记录 ID，响应须回带它做关联；也是幂等键 |
| `service` | string | 用户配置的 service 名（如 `openai-chat-completions`） |
| `endpoint_type` | string | 协议类型：`openai-chat-completions` / `openai-responses` / `anthropic-messages` |
| `upstream` | string | 本条流量的真实上游（如 `https://api.openai.com`） |
| `path` | string | 请求路径（如 `/v1/chat/completions`） |
| `timestamp` | string (ISO8601) | 请求发生时间（UTC，带时区偏移） |
| `elapsed_ms` | int | 上游耗时（毫秒） |
| `status_code` | int | 上游返回的 HTTP 状态码（上游不可达时为 502） |
| `error` | string \| null | 非 null 表示代理层错误（如 `upstream error: ...`） |
| `request` | object | **归一化请求体**（见 3.3） |
| `response` | object | **归一化响应体**（见 3.3） |

### 3.3 request / response 归一化结构

不同 `endpoint_type` 的原始报文已被 safe-guard 的 adapter 归一化：

**request**（按 endpoint_type 略有差异，共同字段）：

| 字段 | 说明 |
|------|------|
| `model` | 模型名（如 `gpt-4o`、`claude-sonnet-5`） |
| `messages` / `input` | 对话内容（chat 类为 `messages` 数组；responses 类为 `input`） |
| `tools` | 工具定义（可能为 null） |
| `stream` | 是否流式 |

**response**：

| 字段 | 说明 |
|------|------|
| `content` | 重组后的完整回答文本（SSE 已拼接） |
| `finish_reason` | 结束原因（`stop` / `length` 等） |
| `usage.prompt_tokens` / `completion_tokens` / `total_tokens` | token 用量 |
| `raw` | 原始报文（通常为 null，保留字段） |

> **注意**：`record_body` 配置为 false 时 request/response 只含元数据骨架（无内容）——检测逻辑如依赖内容需要用户开启 `record_body`。

---

## 4. 响应格式

### 4.1 成功（HTTP 200）

```json
{
  "results": [
    {
      "record_id": "e0dd6216-8518-4634-a5e0-f97fd7672c52",
      "detection_status": "violation",
      "risk_level": "high",
      "detection_detail": {
        "score": 0.949,
        "reason": "prompt_injection",
        "matched_rules": ["rule-e08eda34"],
        "trace_id": "c37b20e9..."
      },
      "detected_at": "2026-08-21T02:34:06.069312+00:00"
    }
  ]
}
```

| 字段 | 必填 | 类型 | 说明 |
|------|------|------|------|
| `results` | ✅ | array | 与 batch 一一对应的结论数组 |
| `results[i].record_id` | ✅ | string | **必须回带**请求中的 record_id——safe-guard 用它把结论关联回本地记录；**无法识别的 record_id 会被忽略** |
| `results[i].detection_status` | ✅ | string | 枚举：`clean` / `suspicious` / `violation` / `error`（见 4.2） |
| `results[i].risk_level` | 推荐 | string \| null | 枚举：`low` / `medium` / `high` / `critical`；`clean` 时一般 `low` 或 null |
| `results[i].detection_detail` | 推荐 | object | 自由结构（safe-guard 原样存 SQLite，`report --json` 透出）。建议含 `score`、`reason`、规则命中 |
| `results[i].detected_at` | ✅ | string (ISO8601) | 检测完成时间 |

### 4.2 detection_status 语义

| 值 | 语义 | safe-guard 侧行为 |
|----|------|-------------------|
| `clean` | 无风险 | 正常入库 |
| `suspicious` | 疑似（人工复核） | 正常入库（用户可用 `report --json` 过滤） |
| `violation` | 违规 | 正常入库；**不阻断流量**（检测是事后审计，代理透明转发） |
| `error` | 检测本身失败 | 正常入库，用户可见 |

> safe-guard **不因 violation 阻断请求**——大模型响应早已透传给用户。检测结论用于审计与告警，若需实时阻断需另行设计。

### 4.3 部分 record 无结论

允许 `results` 数量 < `batch` 数量（比如内部超时先返回已有结论）。**缺失的 record_id 不会被入库，也不会重试单条**——如需保证完整返回，请等全部检测完再响应（在超时预算内，见 §7）。

---

## 5. 状态码与重试语义

| 状态码 | safe-guard 分类 | 行为 |
|--------|----------------|------|
| 200 | 成功 | 游标推进，记录入库 |
| 401 / 403 | `AUTH` | **停止上报循环**，日志提示用户重新配置 api_key |
| 400–499（其他） | `PAYLOAD` | 记录保留重试（可能 payload 与服务端解析不兼容） |
| ≥ 500 | `SERVER` | 记录保留重试，**指数退避**（2s → 4s → … → 上限 60s） |
| 网络错误 / 超时 | `SERVER` | 同上 |

**对服务端的含义**：

1. 5xx / 超时会触发重试——**同一批记录可能被多次收到**（见幂等性 §6）
2. 4xx（非 401/403）也会重试——如果你的服务端认为请求不可恢复，请返回 401/403（如果真是鉴权问题）或修正解析逻辑
3. 重试期间 safe-guard 停止拉取新批（内存保护），所以**长时间不可用会积压在用户本地**，恢复后集中补报

---

## 6. 幂等性

- `record_id` 是 UUID，同一逻辑记录重试/重报时 **record_id 不变**
- safe-guard 本地 SQLite 对 `record_id` 做 UPSERT（重复结论覆盖旧结论）
- **服务端要求**：按 `record_id` 幂等处理（重复收到同一条 Record，覆盖或忽略均可，不要产生重复告警）
- 用户还可 `safe-guard redo <record_id>` 手动重报任意历史记录——**任何时候都可能收到旧记录**

---

## 7. 流量特征

| 项 | 值 | 说明 |
|----|----|------|
| 上报周期 | 默认 60s（用户可配，调试时可能 1-5s） | 每周期一次 POST |
| 批量大小 | 默认 ≤500 条/批 | 用户可配 |
| 请求体大小 | 典型 1-100 KB，极端可达数 MB（长对话） | Record 含完整对话内容 |
| 客户端超时 | **30 秒** | 超时即断开重试；检测逻辑请在此预算内完成（异步检测可先返回 `error`/`suspicious` 结论，后台完成后靠 redo） |
| 并发 | 单用户单进程串行上报 | 无并发竞争 |
| User-Agent | aiohttp 默认 | — |

---

## 8. 最小实现参考

仓库自带一个可运行的 mock（**FastAPI**），可直接参考或当作对接前的联调桩：

- 代码：[`tests/mock_detector/server.py`](../tests/mock_detector/server.py)
- 启动：`uvicorn server:app --app-dir tests/mock_detector --host 127.0.0.1 --port 8000`
- 它按 5% 概率随机标 violation，含 `GET /records` 查询端点

核心逻辑摘录（约 30 行即可对接）：

```python
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()
API_KEY = "your-key"

@app.post("/detect")
async def detect(request: Request):
    if request.headers.get("X-API-Key") != API_KEY:
        raise HTTPException(status_code=401, detail="unauthorized")

    body = await request.json()
    results = []
    for record in body["batch"]:
        # ↓↓↓ 你的检测逻辑（对 record["request"] / record["response"] 做分析）↓↓↓
        is_violation = my_detect(record)
        results.append({
            "record_id": record["record_id"],          # 必须回带
            "detection_status": "violation" if is_violation else "clean",
            "risk_level": "high" if is_violation else "low",
            "detection_detail": {"reason": "..."},
            "detected_at": "2026-08-21T02:34:06Z",     # ISO8601
        })
    return {"results": results}
```

curl 自测（模拟 safe-guard 的请求）：

```bash
curl -X POST http://your-server:8080/detect \
  -H "X-API-Key: your-key" \
  -H "Content-Type: application/json" \
  -d '{
    "batch": [{
      "record_id": "e0dd6216-8518-4634-a5e0-f97fd7672c52",
      "service": "openai-chat-completions",
      "endpoint_type": "openai-chat-completions",
      "upstream": "https://api.openai.com",
      "path": "/v1/chat/completions",
      "timestamp": "2026-08-21T01:45:01.675+00:00",
      "elapsed_ms": 2,
      "status_code": 200,
      "error": null,
      "request": {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}], "tools": null, "stream": false},
      "response": {"content": "Hello world", "finish_reason": "stop", "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}, "raw": null}
    }]
  }'
```

---

## 9. 常见问题

**Q1：收到重复的 record_id？**
正常。重试 / redo / 崩溃续传都会重发。按 §6 幂等处理。

**Q2：batch 里夹着 `error` 非 null / `status_code` 502 的记录？**
代理层错误也有记录价值（比如请求内容本身导致上游拒绝）。是否检测由你们决定，但**必须回带 record_id**（哪怕结论是 `error`）。

**Q3：用户把 endpoint_path 配成你们的自定义路径后 404？**
确认服务端路由注册的路径与 `detector.endpoint_path` 完全一致（含大小写、前导 `/`）。safe-guard 侧拼接规则：`url.rstrip("/") + endpoint_path`。

**Q4：想让 safe-guard 停止重试？**
只有 401/403 会让它停止上报循环。5xx 会无限退避重试（上限 60s 间隔）——这是有意的（网络抖动不丢数据）。

**Q5：响应必须全量吗？可以先返回部分结果吗？**
可以（见 §4.3），但缺失的记录不会单条补拉。如需完整，请在 30s 超时预算内返回全量。

---

## 附：契约变更流程

本文档描述的是 **v1 契约**（safe-guard 0.1.x）。若你们的接口需要不兼容变更（改字段名/鉴权方式/路径语义），请提前协调——safe-guard 侧通过 `config_version` + adapter 层做版本适配。

---

## 附：实现建议（非契约要求）——同会话前缀去重

> 这是给检测服务器开发方的**性能优化建议**，不影响协议——CLI 始终发送完整快照，去重是 detector 的内部自由。

**问题**：完整快照语义下，单会话第 N 条 Record 包含前 N-1 轮全部对话内容。若检测采用 LLM 且对每条 Record 独立全量送审，同一轮历史会被反复审读，总审读量为 **O(N²)**——50 轮会话约 96% 的 token 花在重复内容上。

**建议机制**（前缀 hash 缓存，参考实现见 `tests/mock_detector/server.py` 的 llm 模式）：

```
对 request.messages 算 hash 链：h_k = sha256(h_{k-1} ‖ canonical_json(m_k))
收到 Record → 找最长已审前缀 h_k（缓存命中）
  ├─ 整条命中（会话重放）→ 不调 LLM，直接复用缓存结论
  └─ 部分命中 → 只把 messages[k+1..] 新增轮次送 LLM
                 prompt 附一行："前情 k 轮已审：<结论>"（保留跨轮语义判断力）
结论写缓存（该会话每个前缀 hash → 本次综合结论）
```

**实测数据**（mock llm 模式 + 真实 LLM，10 轮递增会话）：110 轮到达仅送审 20 轮，**节省 81.8%**；50 轮会话理论节省 ~96%（审读量 O(N²)→O(N)）。

**要点**：
- hash 键用内容级 canonical JSON——天然幂等，`redo` 重报时前缀照样命中，只重审新增（行为更正确）
- "前情已审结论"一行是必要的：新轮次可能引用旧轮次（如"执行上面第 1 轮的指令"），完全丢弃上下文会漏检跨轮攻击
- 缓存 TTL 建议对齐 safe-guard 侧 `purge` 周期；检测规则/模型升级时应主动失效（允许重审全量）
- 该优化与传输层 gzip（`Content-Encoding: gzip`，高重复文本压缩比通常 10-20%）互补：去重省 LLM token，gzip 省网络带宽，二者可叠加
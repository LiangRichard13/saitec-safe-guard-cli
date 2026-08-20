# Mock 检测服务器

模拟单位内部安全检测服务的 FastAPI 服务器，供 `safe-guard` 本地联调用。

- 接收 `POST /detect`，按 **5% 概率**（可调）把记录标为 `violation / high|critical`，其余 `clean / low`
- `GET /records` 查询已处理记录
- `GET /health` 健康检查

## 启动

```bash
cd test/mock_detector
pip install -r requirements.txt

# 方式 1：uvicorn（推荐）
uvicorn server:app --host 127.0.0.1 --port 8000

# 方式 2：直接跑脚本
python server.py
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `MOCK_DETECTION_VIOLATION_RATE` | `0.05` | 危险标记概率（0~1） |
| `MOCK_DETECTION_LATENCY_MS` | `50` | 模拟处理延迟（毫秒） |
| `MOCK_DETECTION_API_KEY` | `mock-test-key` | 期望的 `X-API-Key` |
| `MOCK_DETECTION_HOST` | `127.0.0.1` | 监听地址（仅 `python server.py` 生效） |
| `MOCK_DETECTION_PORT` | `8000` | 监听端口（仅 `python server.py` 生效） |

## 与 safe-guard 集成

```bash
# 1. 起 mock detector（另一个终端）
uvicorn server:app --host 127.0.0.1 --port 8000

# 2. 用 mock 地址初始化 safe-guard
safe-guard init --detector-url http://127.0.0.1:8000 --api-key mock-test-key

# 3. 启动 safe-guard（后台代理 + 定时上报）
safe-guard start

# 4. 发请求到本地代理端口（默认 9001 = openai-chat-completions）
curl http://127.0.0.1:9001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"hello"}]}'

# 5. 等 report_interval_sec（默认 60s）后查看
curl http://127.0.0.1:8000/records | python -m json.tool   # mock 侧
safe-guard report --json                                       # safe-guard 本地 SQLite
```

> 想立刻看到上报结果，可先 `safe-guard config set detector.report_interval_sec 5` 再 `safe-guard restart`。

## 手动测试端点

```bash
# 健康检查
curl http://127.0.0.1:8000/health

# 上报（单条）
curl -X POST http://127.0.0.1:8000/detect \
  -H "X-API-Key: mock-test-key" \
  -H "Content-Type: application/json" \
  -d '{"batch":[{"record_id":"r1","service":"test","endpoint_type":"openai-chat-completions","upstream":"x","path":"/v1","timestamp":"2026-08-20T10:00:00Z","elapsed_ms":100,"status_code":200,"request":{},"response":{}}]}'

# 查询已处理（按 risk 过滤）
curl "http://127.0.0.1:8000/records?risk=high"
curl "http://127.0.0.1:8000/records?service=test&risk=critical"

# 错误 api_key → 401
curl -X POST http://127.0.0.1:8000/detect \
  -H "X-API-Key: wrong" -H "Content-Type: application/json" -d '{"batch":[]}'
```

## 验证 5% 概率

```bash
for i in $(seq 1 20); do
  curl -s -X POST http://127.0.0.1:8000/detect \
    -H "X-API-Key: mock-test-key" -H "Content-Type: application/json" \
    -d '{"batch":[{"record_id":"r'$i'","service":"test","endpoint_type":"openai-chat-completions","upstream":"x","path":"/v1","timestamp":"2026-08-20T10:00:00Z","elapsed_ms":100,"status_code":200,"request":{},"response":{}}]}' > /dev/null
done
curl -s http://127.0.0.1:8000/records | python -c "import sys,json; d=json.load(sys.stdin); print(f'count={d[\"count\"]} violations={d[\"violations\"]}')"
# 期望约 1/20 条标 violation（概率性，可能有波动）
```

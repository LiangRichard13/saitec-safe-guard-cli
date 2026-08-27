# ssgc 深入操作参考

SKILL.md 覆盖高频操作；本文件是排错手册与低频操作细节。按需阅读对应小节。

## 目录
1. [完整排错手册](#1-完整排错手册)
2. [mock detector 联调](#2-mock-detector-联调)
3. [redo / purge / 日志](#3-redo--purge--日志)
4. [detector 对接契约摘要](#4-detector-对接契约摘要)
5. [配置体系细节](#5-配置体系细节)

---

## 1. 完整排错手册

### start 后 status 显示未运行
看 `{config_dir}/logs/ssgc.log` 尾部。最常见：端口被占（`Errno 10048`）——另一个 ssgc 实例还在（可能用了不同 SSGC_CONFIG），`stop` 它或给新实例换端口。

### ALREADY_RUNNING 但实际没在跑
PID 文件残留（进程死了没清）。删 `{config_dir}/ssgc.pid` 后重试。`stop` 命令自身也能清理（报 STALE_PID 并自动删）。

### detector 401 / 上报停摆
日志出现 `X-API-Key 失效，停止上报：auth failed (401) at <url>`。这是**故意停摆**（避免无限重试锁死账号）。修复：

```bash
ssgc config set detector.api_key NEW_KEY --json
ssgc restart --json
```

未上报的记录不会丢：JSONL 落盘先于上报，重启后 `_replay_unreported` 按游标自动续传。

### detector 5xx / 不可达
指数退避重试（2s→4s→…→上限 60s），pending 批保留不丢。恢复后自动补报。无需干预，除非长时间不可达——查 `ssgc logs --tail 50` 的 `report failed (kind=SERVER)`。

### report 空结果三连查
1. 时间窗口：`--since 30m` 改 `--since 7d`
2. 上报周期：默认 60s，刚发的请求等一个周期
3. 根本没记录：`status --json` 确认 running，客户端确实把请求发到了本地端口（检查 base_url）

### SQLite 损坏（doctor 报 sqlite fail）
磁盘满或权限问题居多。**不要直接删 results.db**（丢历史）。备份后重命名让 CLI 重建：

```bash
mv {config_dir}/results.db {config_dir}/results.db.corrupt
ssgc restart --json
```

JSONL 是完整原始记录，历史可从 JSONL 重报（`redo`）恢复。

### Windows 中文乱码
CLI 在 pipe 模式已强制 UTF-8。若 Agent 捕获到乱码，检查是否走了老 cmd（`chcp 65001`）或输出编码被 shell 二次转换。`--json` 输出不受影响（UTF-8 JSON）。

### 端口被谁占了

```bash
netstat -ano | grep ":9001" | grep LISTENING    # 拿 PID
tasklist //FI "PID eq <pid>"                     # Windows 看进程名
```

---

## 2. mock detector 联调

无真实检测服务器时，用仓库自带 mock（`tests/mock_detector/`）：

```bash
# random 模式：5% 概率随机标 violation（无需配置）
uvicorn server:app --app-dir tests/mock_detector --host 127.0.0.1 --port 8000 &

# llm 模式：真实大模型判定（tests/mock_detector/.env 配 MOCK_LLM_API_KEY 等）
MOCK_DETECTION_MODE=llm uvicorn server:app --app-dir tests/mock_detector --host 127.0.0.1 --port 8000 &
```

接入：`ssgc init --api-key mock-test-key --detector-url http://127.0.0.1:8000 --upstream <端点>`。

验证上报到达：`curl -s http://127.0.0.1:8000/records | python -m json.tool`（mock 内存里的记录）。

联调脚本：`python tests/test_chat/chat_probe.py [N]`（经代理发 N 条混合内容消息；配置在 `tests/test_chat/.env`）。

---

## 3. redo / purge / 日志

### redo（重报单条）

```bash
ssgc report --since 7d --json | jq -r '.data.results[0].record_id'   # 拿全量 record_id
ssgc redo <record_id> --json
```

用途：detector 规则更新后重新评估历史记录。record_id 从 JSONL（`{config_dir}/records/records-*.jsonl`）或 report 查。

### purge（清理）

```bash
ssgc purge --retention-days 30 --dry-run --json   # 预览
ssgc purge --retention-days 30 --json             # 执行
```

清理三类：超期 JSONL、日志切割备份（`ssgc.log.YYYY-MM-DD`）、SQLite 超期行。活跃日志文件和未超期数据不动。

### 日志

- 文件：`{config_dir}/logs/ssgc.log`（每日午夜切割，自动保留 14 天）
- CLI：`ssgc logs --tail 100 [--service NAME]`（子串过滤）
- 上报循环的关键日志行：`runtime started` / `report failed (kind=...)` / `X-API-Key 失效` / `runtime stopped`

### tail（实时跟新记录）

`ssgc tail [--service NAME]`：从启动时刻起跟踪当日 `records-*.jsonl` 的**新增行**（跳过历史），每行一条 Record JSON。首行 `# tailing <file>` 是注释，解析时跳过 `#` 开头行；Ctrl+C 退出。适合"实时感知新流量"（Agent 挂后台跟读）；violation 结论仍滞后一个上报周期，结论查询用 `report`。注意 `--level` 过滤匹配的 `level` 字段在 Record 中不存在（对 records 流无效）。

---

## 4. detector 对接契约摘要

完整契约见 `docs/integration/detector-api.md`。Agent 视角要点：

- 请求：`POST {url}{endpoint_path}`，头 `X-API-Key`，体 `{"batch": [Record...]}`（Record 含完整归一化 request/response）
- 响应：`{"results": [{"record_id", "detection_status", "risk_level", "detection_detail", "detected_at"}]}`
- record_id 必须回带；未知 id 被忽略；幂等（重复上报安全）
- 401/403 → CLI 停止上报；其他 4xx/5xx/超时 → 重试退避
- 服务端响应预算 30s（超时即重试）

---

## 5. 配置体系细节

### 三级优先级
CLI 参数 > 环境变量（`SSGC_*`） > config.json。

常用环境变量：`SSGC_CONFIG`（配置路径）· `SSGC_API_KEY` · `SSGC_DETECTOR_URL` · `SSGC_ENDPOINT_PATH` · `SSGC_REPORT_INTERVAL` · `SSGC_BATCH_SIZE` · `SSGC_LOG_LEVEL` · `SSGC_<SERVICE_NAME>_UPSTREAM/PORT`（service 级覆盖）。

### config.json 结构（dot-path 操作对象）

```json
{
  "detector": {"url", "api_key", "endpoint_path", "report_interval_sec", "batch_size", "max_queue_size"},
  "services": [{"name", "port", "upstream", "endpoint_type", "record_body"}],
  "log_level": "INFO"
}
```

`config set` 走原子链：备份（`config.json.bak.<时间戳>`）→ 校验（失败不写入）→ 落盘。api_key 在 get/list 输出中自动脱敏（`***`）。

### 数据文件布局（跟随 config 目录）

```
{config_dir}/
├── config.json
├── ssgc.pid            # 运行时 PID
├── records/*.jsonl           # 原始记录（按天分片，崩溃恢复源）
├── results.db                # 检测结果（SQLite WAL）
└── logs/ssgc.log[.YYYY-MM-DD]
```

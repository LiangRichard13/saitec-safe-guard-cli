# SSGC 双重验证清单

> **何时执行**：每次重大改动后（新功能 / P0 修复 / 重命名）跑一轮完整验证。
> **目标**：深度验证（语义正确性）+ 广度验证（命令全覆盖），弥补 bash 冒烟只查 exit code 和 JSON 结构的盲区。
> **原则**：Agent 必须**亲自阅读内容**，不能只断言 exit code == 0。

---

## 一、深度验证（语义正确性）

> 核心问题：数据说的对不对？请求和响应是否匹配？字段是否完整？

### D1. 三协议探针执行

分别跑三个探针脚本，确认全绿或已知可接受失败：

```bash
# openai-chat-completions（端口 9001）
TEST_BASE_URL=http://127.0.0.1:9001/v1 python tests/test_chat/chat_probe.py

# anthropic-messages（端口 9002）
TEST_BASE_URL=http://127.0.0.1:9002 python tests/test_chat/probe_anthropic_messages.py

# openai-responses（端口 9003，需上游支持，可跳过）
TEST_BASE_URL=http://127.0.0.1:9003/v1 python tests/test_chat/probe_openai_responses.py
```

**判定标准**：
- [ ] 每个探针成功率 ≥ 10/12（允许个别上游超时）
- [ ] 控制台输出中，每条响应的**前 40 字与问题语义相关**（问"1+1"不能答自我介绍）

### D2. JSONL 语义对齐检查

等上报周期（60s）后，逐条检查最新 JSONL 文件：

```python
import json
records = [json.loads(l) for l in open(f"~/.ssgc/records/records-{today}.jsonl")]
```

**逐条检查项**：
- [ ] `request.messages` 非空，最后一条 user 消息内容可见
- [ ] `response.content` 非空（除故意空响应的记录）
- [ ] **关键：任意两条不同请求的 response.content 不相同**（adapter 状态残留检测）
- [ ] `response.usage` 有 prompt_tokens 和 completion_tokens
- [ ] `response.finish_reason` 在合理枚举内（stop/end_turn/max_tokens/length/completed）
- [ ] `elapsed_ms` > 0
- [ ] `status_code` == 200（正常记录）或 502（上游不可达）
- [ ] 字段完整性：`service` / `endpoint_type` / `upstream` / `path` / `timestamp` 全部非空

### D3. report 输出验证

```bash
ssgc report --since 30m --json --limit 50
```

**检查项**：
- [ ] `data.count` 与 JSONL 记录数一致（或合理子集）
- [ ] 每条 result 包含新字段：`upstream`、`endpoint_type`、`finish_reason`
- [ ] `detail.reason` 内容与该条记录的实际请求相关（检测器没判错）
- [ ] 人类模式 `ssgc report --since 30m` 表格渲染正常、无乱码

### D4. export 报告验证

```bash
ssgc export -f md -o /tmp/verify-report.md
ssgc export -f html -o /tmp/verify-report.html
```

**检查项**：
- [ ] 两个文件都生成成功（非空）
- [ ] **md 报告**：汇总表有 upstream 列；详情表有"上游端点""协议/路径""结束原因"行
- [ ] **md 报告**：对话区块中 `[user]` 内容与 `[assistant 回复]` 语义相关
- [ ] **html 报告**：浏览器打开渲染正常（可用 Chrome headless 截图验证）
- [ ] `--status all` 导出条数 > 默认异常导出条数（clean 记录确实被默认过滤）
- [ ] `--json` 摘要中 `by_status` 分布合理

### D5. 日志健康检查

```bash
ssgc logs --tail 100
```

**检查项**：
- [ ] 无未预期的 `ERROR` / `Traceback` 行
- [ ] `runtime started` / `proxy started` 生命周期事件正常
- [ ] 无 `X-API-Key 失效` 报错
- [ ] 无 `report failed (kind=` 退避报错（允许偶发 SERVER 重试）

### D6. SQLite 数据完整性

```python
import sqlite3
conn = sqlite3.connect("~/.ssgc/results.db")
# 检查字段非空率
conn.execute("SELECT COUNT(*) FROM detection_results WHERE upstream IS NULL OR upstream = ''").fetchone()
conn.execute("SELECT COUNT(*) FROM detection_results WHERE endpoint_type IS NULL").fetchone()
conn.execute("SELECT COUNT(*) FROM detection_results WHERE finish_reason IS NULL").fetchone()
```

**检查项**：
- [ ] upstream 非空率 100%
- [ ] endpoint_type 非空率 100%
- [ ] finish_reason 非空率 > 90%（允许个别上游异常记录为空）
- [ ] detection_status 分布合理（clean 占多数，violation/suspicious 少数）

---

## 二、广度验证（命令全覆盖）

> 核心问题：每个命令能不能用？人类模式和 --json 模式都正常吗？

### B1. 配置类命令

| # | 命令 | 人类模式 | --json 模式 | 边界/错误路径 |
|---|------|----------|------------|-------------|
| 1 | `ssgc validate` | 输出 "config valid" | `{"ok":true}` | 故意改坏 config 后报错 |
| 2 | `ssgc config list` | 表格输出 | 结构化 dict | — |
| 3 | `ssgc config get detector.url` | 单行值 | `{"data":"..."}` | 不存在的 key 报错 |
| 4 | `ssgc config set log_level debug` | 确认写入 | `{"ok":true}` | 无效值报错 |
| 5 | `ssgc config unset log_level` | 确认删除 | `{"ok":true}` | 不存在的 key 报错 |
| 6 | `ssgc service list` | 服务映射表 | 结构化数组 | — |
| 7 | `ssgc service add test-svc --upstream http://localhost:9999 --json` | — | `{"ok":true}` | name 重名报 NAME_EXISTS |
| 8 | `ssgc service set test-svc --port 9099 --json` | — | `{"ok":true}` | 不存在的 name 报错 |
| 9 | `ssgc service remove test-svc --json` | — | `{"ok":true}` | 不存在的 name 报错 |
| 10 | `ssgc init --force ...` | 完整输出 | `{"ok":true}` | 缺 --upstream 报错 |

### B2. 生命周期命令

| # | 命令 | 人类模式 | --json 模式 | 边界/错误路径 |
|---|------|----------|------------|-------------|
| 11 | `ssgc stop` | 确认停止 | `{"data":{"stopped":true}}` | 已停止时 STALE_PID 自愈 |
| 12 | `ssgc start` | banner + 服务映射 | `{"ok":true,"data":{"pid":N}}` | 已运行时 ALREADY_RUNNING |
| 13 | `ssgc status` | 运行状态表 | `data.running` 字段 | — |
| 14 | `ssgc restart` | stop + start | `{"ok":true}` | — |
| 15 | `ssgc monitor` | 前台实时输出 | 不支持 --json | Ctrl+C 退出 + 会话总结 |

### B3. 运维命令

| # | 命令 | 人类模式 | --json 模式 | 边界/错误路径 |
|---|------|----------|------------|-------------|
| 16 | `ssgc report --since 1h` | 彩色表格 | `data.results[]` | 空窗口无数据 |
| 17 | `ssgc report --service X --limit 10` | 过滤结果 | 同上 | 不存在的 service 返回空 |
| 18 | `ssgc export -f md` | 生成文件 + 摘要 | `data.output_path` | 无效 format 报错 |
| 19 | `ssgc export -f html --status all` | 全量导出 | 同上 | — |
| 20 | `ssgc redo <record_id>` | 确认重报 | `data.detection_status` | 不存在的 id 报 RECORD_NOT_FOUND |
| 21 | `ssgc purge --dry-run` | 预览要删的文件 | `data.files` | — |

### B4. 调试命令

| # | 命令 | 人类模式 | --json 模式 | 边界/错误路径 |
|---|------|----------|------------|-------------|
| 22 | `ssgc doctor` | 六项自检表 | 各项 status | — |
| 23 | `ssgc doctor --quick` | 跳过 API 探测 | 同上 | — |
| 24 | `ssgc logs --tail 20` | 最近 20 行日志 | 同文本 | — |
| 25 | `ssgc logs --follow` | 实时 tail | — | Ctrl+C 退出 |
| 26 | `ssgc tail` | 实时 JSONL 流 | — | Ctrl+C 退出 |

### B5. --config 隔离验证

```bash
# 用临时配置目录验证 --config 参数生效
export SSGC_CONFIG=/tmp/ssgc-verify-XXXXX/config.json
ssgc init --api-key test-key-12345678 --detector-url http://127.0.0.1:8001 --upstream http://localhost:23333 --json
ssgc validate --json    # 应 ok:true
ssgc start --json       # 应成功启动
ssgc stop --json        # 应成功停止
# 清理临时目录
```

**检查项**：
- [ ] 隔离目录下的 config.json 正确创建
- [ ] start/stop 在隔离目录下正常工作（PID/log 文件在隔离目录）

---

## 三、执行顺序

1. **先深度**（D1→D6）：确保核心数据链路语义正确
2. **后广度**（B1→B5）：确保所有命令可用
3. **最终**：全量 pytest 250+ 绿

预计耗时：深度 ~15min（含上报等待），广度 ~10min。

---

## 四、已知可接受的"失败"

| 场景 | 现象 | 原因 |
|------|------|------|
| openai-responses 探针 | 全部失败 | 无支持 Responses API 的上游（待补测） |
| probe 个别条目 | 空 response / Connection error | 上游偶发超时，不视为 bug |
| `ssgc monitor` 的 `--json` | 不支持 | 设计如此（人盯场景不需要 JSON） |
| 日志偶发 `report failed (kind=SERVER)` | 退避重试后恢复 | detector 偶发不可用，正常退避 |

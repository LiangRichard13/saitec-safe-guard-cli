# AGENTS.md — 项目记忆（开发约定）

> **分工**：开发约定看本文档；项目进度看 [PROGRESS.md](PROGRESS.md)（按需读取）；CLI 使用方法看 `.claude/skills/ssgc/SKILL.md` 与 `docs/user-guide.md`；检测服务器对接看 `docs/integration/detector-api.md`。本文档不重复它们的内容。
>
> **Claude Code 桥接**：本项目约定统一写在 AGENTS.md（跨工具标准）。Claude Code 不自动读它——本地建一个 `CLAUDE.md` 内容仅一行 `@AGENTS.md` 即可（import 语法，已 gitignore 不入库）。

## 1. 项目背景

`ssgc` 是反向代理 CLI：把大模型 API 请求（OpenAI Chat Completions / OpenAI Responses / Anthropic Messages 及任意兼容端点）指到本地端口，透明转发到真实上游，同时记录请求/响应（JSONL）、周期上报到内部安全检测服务器、检测结果落本地 SQLite。

- 技术栈：Python ≥3.10 / aiohttp / typer / rich / SQLite(WAL) / pytest；包名 `saitec-safe-guard-cli`，CLI 命令 `ssgc`
- 单机自用、本地 http 明文（无 MITM）、检测是事后审计（不阻断流量）
- 当前状态与功能演进见 PROGRESS.md

## 2. 目录结构

```
src/ssgc/                  # 六层架构，依赖只能向下（见 §3）
├── core/                    # L1 最底层：models(数据模型) config(加载/校验/三级覆盖)
│                            #    paths(路径解析 ~/.ssgc) utils(纯函数)
├── recorder/                # L2：recorder.py（内存队列 + JSONL 按天落盘，崩溃恢复源）
├── reporter/                # L2：reporter.py（HTTP 批量上报，X-API-Key，错误三分类 AUTH/PAYLOAD/SERVER）
├── store/                   # L2：store.py（SQLite WAL：detection_results + report_cursor 游标）
├── adapters/                # L3：base.py(Adapter 抽象) + 三协议各一文件
│                            #    （SSE 流重组 + 非流式解析 + 请求归一化）
├── proxy/                   # L4：server.py（ProxyService：catch-all 转发 + 透传 + 限长）
├── runtime/                 # L5：runtime.py（唯一编排者：start/stop/上报循环/续传/event_sink）
└── cli/                     # L6：main.py(typer app) _common.py(emit/视觉/PID/路径)
                             #    _serve.py(start 的子进程入口) commands/(14 个命令一文件一命令)

tests/                       # 镜像 src 分层
├── test_core|test_recorder|test_reporter|test_store|test_adapters|test_proxy|test_runtime|test_cli/
├── test_mock/               # mock_detector 自身的接口测试
├── mock_detector/           # 联调用 mock 检测服务器（random / llm 两种模式，.env 不提交）
└── test_chat/chat_probe.py  # 手动联调脚本：经代理发真实消息验证链路（pytest 不收集）

docs/
├── user-guide.md            # 人类用户手册（命令/配置/排错/安全）
├── how-it-works.md          # 实现原理通俗导览（数据流/六层/关键机制/设计取舍）
├── design/                  # design.md(总体) architecture.md(六层) data-model.md(SQLite+JSONL)
├── integration/detector-api.md  # 检测服务器对接契约（含去重实现建议）
└── issues/cli-usage-issues.md   # 历史问题清单

.claude/skills/ssgc/  # 教 Agent 操作本 CLI 的 skill（SKILL.md + references + evals）
```

## 3. 分层原则（维护扩展的铁律）

依赖严格单向：`L6 cli → L5 runtime → L4 proxy / L3 adapters → L2 recorder·reporter·store → L1 core`。只准 import 下层，禁止反向/跨层跳跃。

**新功能落在哪层**：

| 改动类型 | 落点（全部要改的地方） |
|---|---|
| 新协议端点 | `adapters/` 新文件（继承 base.Adapter）+ `adapters/__init__.py` 注册 get_adapter + `core/config.py` 的 `_VALID_ENDPOINT_TYPES` |
| 新 CLI 命令 | `cli/commands/` 新文件 + `cli/main.py` 注册 |
| 新配置字段（detector 级） | `core/models.py`(dataclass) + `core/config.py`(解析/校验/`_DETECTOR_ENV_MAP`/`_CLI_FIELD_MAP`) + `cli/commands/config_cmd.py`(`_raw_to_model` + `_env_var_for_field`) + `cli/commands/init.py`(默认写入) —— 四处同步，漏一处即 bug |
| 新配置字段（service 级） | 同上 + `service_cmd.py` 的 set 选项 |
| monitor 类实时事件 | `runtime.py` 加 `_emit_event` 事件点 +（流量类）`proxy/server.py` 的 event_sink 透传 |
| 上报契约变化 | `reporter/reporter.py` + `docs/integration/detector-api.md` 同步更新 |

**通用原则**：
- 层间只经构造函数注入（如 Runtime 持有 Recorder/Reporter/Store；ProxyService 收 adapter/recorder/sink 均为参数），不模块级互相 import
- 不动 proxy 的转发路径与透明性（监控不得改变转发行为）
- `--json` 输出是 Agent 契约：字段只增不减、含义不变；人类可读输出随意美化
- 错误路径也要产生 Record（审计完整性）；后台循环不许被异常杀死（兜底 + 退避）
- 测试放镜像子目录（`tests/test_<layer>/`）；mock/手写脚本放 `tests/mock_detector/`、`tests/test_chat/`，命名避开 `test_*` 前缀防 pytest 误收集

## 4. 文档维护矩阵

功能改动提交前，按下表核对要同步的文档（漏更即文档债）：

| 改动类型 | README | user-guide | SKILL.md | detector-api.md |
|---|---|---|---|---|
| 新命令/新参数 | 命令速查表 + 命令计数 | §4 对应命令小节 | 命令速查 + 相关 recipe | — |
| 契约/上报行为变化 | — | — | — | 对应章节 |
| 架构/分层调整 | 项目结构节 | — | — | —（改 AGENTS.md §2/§3 + design/architecture.md） |
| CLI 操作行为变化（用户可感知） | — | §7 排错手册 | 排错决策树 + references/operations.md | FAQ |
| bug 修复（操作行为不变） | — | **不更新** | **不更新** | — |
| 配置新字段 | — | §5 配置表 + env var 列表 | 配置相关段落 | （detector 字段时）请求格式 |

> **原则**：已修复的 bug 用户不会再遇到，不写排错条目教用户排一个不存在的错。bug 修复记录进 `docs/issues/cli-usage-issues.md` 与 PROGRESS.md 即可；user-guide / SKILL 只在**CLI 操作行为变化**（命令语义、参数、输出、需用户动手的差异）时更新。

## 5. PROGRESS.md 约定

- **位置**：项目根；Agent 按需读取（不固定加载，允许变长）
- **格式**：倒序时间线，每条：

  ```markdown
  ## YYYY-MM-DD  <feat|fix|docs|refactor|infra>
  一到三句：做了什么、为什么。<commit-hash>
  教训:（可选一行）坑的根因，防止下一个 Agent 重踩。
  ```

- **记什么**：完成的功能点（带 commit）、遇到的坑与根因、重要设计取舍
- **更新时机**：每次 push 后立即追加（commit 与条目一一对应）；**接手 Agent 应先读它**建立上下文，再读本文档

## 6. Git 约定

- **commit 格式**：`type(scope): 中文摘要`（type ∈ feat|fix|docs|test|refactor|chore），body 列要点与验证结论；现有风格见 `git log`
- **频率**：一个 feature / 一个 bugfix 一提交，**勿攒大包**；文档更新跟随对应功能同提或紧随补提
- **分支**：默认直接 master（单人项目）；仅当一个抽象大模块预计跨多个 commit/多次会话时开 `feat/<模块名>` 分支，完成后再合并
- **push 前置**：全量 pytest 绿（见 §7 命令）；VPN 不可用时本地攒着，恢复后一起推

## 7. 测试与调试

### 单元测试
```bash
# ⚠️ 必须用 saitec-guard conda 环境的全路径（PATH 里没有；base 环境无依赖）
"C:/Users/Administrator/anaconda3/envs/saitec-guard/python.exe" -m pytest tests/ -q
```

### CLI 功能自测（Agent 无需人类介入）
CLI 全命令支持 `--json`（stdout 结构化、stderr 错误、退出码 0/1/2/3/4），用 bash 断言：

```bash
SG="C:/Users/Administrator/anaconda3/envs/saitec-guard/Scripts/ssgc.exe"
export SSGC_CONFIG=/tmp/<隔离目录>/config.json        # 永远用隔离配置，勿动默认配置
"$SG" init --api-key mock-test-key --detector-url http://127.0.0.1:8001 --upstream http://localhost:23333 --json | python -c "import sys,json; assert json.load(sys.stdin)['ok']"
"$SG" status --json | python -c "import sys,json; d=json.load(sys.stdin); assert d['data']['running']"   # 注意：running 判断看字段，exit 0 不代表在跑
```

### 日志与 debug
- 文件：`{config_dir}/logs/ssgc.log`（按天切割，保 14 天）；CLI：`ssgc logs --tail 50`
- 关键 grep：`X-API-Key 失效`（detector 401 → 上报停摆）/ `report failed (kind=)`（上报退避重试）/ `Errno 10048`（端口被占）/ `runtime started|stopped`（生命周期）
- doctor 自检：`ssgc doctor --json`（config/端口/磁盘/SQLite/JSONL 六项）

### 端到端调试设施
- `tests/mock_detector/`：随机模式（无需配置）或 llm 模式（真实 LLM 判定 + 前缀去重，`.env` 配 key 不提交）
- `tests/test_chat/chat_probe.py`：经代理发混合内容消息验证全链路
- 注意：`tests/mock_detector/.env` 存在时 mock 测试自动跳过 import 校验用例并强制 random 模式（设计行为）

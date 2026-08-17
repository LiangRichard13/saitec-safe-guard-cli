# Saitec Safe CLI — 架构设计

- **日期**：2026-08-14
- **状态**：待评审（Draft）
- **配套文档**：`saitec-safe-guard-cli-design.md`（总体设计），本文档专注**架构分层与代码组织**

## 1. 文档目的

把 `saitec-safe-guard-cli-design.md` 描述的工具，按"高内聚、低耦合、依赖单向、底层独立可运行"的原则，拆成清晰的代码层，并定义每层的职责边界、依赖方向、对外接口、目录结构。**本文档不写实现，只定义形态。**

## 2. 设计原则

| 原则 | 含义 |
|---|---|
| **高内聚** | 一层只做一件事，相关代码集中在一个包里 |
| **低耦合** | 层与层之间只通过**显式接口**通信，不共享内部状态 |
| **依赖单向** | **高层 → 低层**；底层**绝不** import 高层 |
| **底层独立可运行** | 最底层（`core`）是一个零依赖、数据模型 + 通用工具的纯 Python 包，可单独 import、被任何高层组合使用 |
| **从最高层开始剥离** | 任何一层都可以被"等价实现"替换（如 `proxy` 替换实现、`reporter` 替换为 Kafka、`cli` 替换为 GUI），不影响其他层 |
| **便于测试** | 每层可独立测试，不依赖上层和其他层 |

### 依赖方向的可视规则

设 `core` 为最底层（layer 0），`cli` 为最高层（layer 7）。则：

- ✅ 允许：`cli → runtime → proxy → adapters → core`
- ❌ 禁止：`core → adapters`（向上跳）、`store → runtime`（反过来）、`adapters → proxy`（向上跳）
- 一个文件只能 import **同层或更低层**的模块

## 3. 分层架构总览

我们采用 **6 层**递进架构（编号 1 为最底层，6 为最顶层）：

```
Layer 6  cli         ──▶  命令行入口、用户交互、输出格式化
                │
Layer 5  runtime     ──▶  异步编排、生命周期、任务调度
                │
Layer 4  proxy       ──▶  反向代理基座、流式透传、错误处理
                │
Layer 3  adapters    ──▶  协议解析、流式重组、归一化记录
                │
Layer 2  recorder / reporter / store  ──▶  三个并行的独立 IO 层
                │
Layer 1  core        ──▶  数据模型、配置 schema、工具函数（零依赖）
```

**每层只出现一次**：

- 8 层原版里 Layer 5 / Layer 4 / Layer 3 / Layer 2 / Layer 1 重复列出了 proxy / adapters / recorder / reporter / store，违反了"一物一编号"原则。本版合并为 6 层，recorder / reporter / store 作为**同一层的三个独立 IO 模块**并列出现。
- Layer 1 `core` 在最底层（任何方向都被依赖）
- Layer 6 `cli` 在最顶层（不被任何业务层 import）

**核心约束**：

- `core` 不依赖任何层 → 永远在最底层
- `cli` 不被任何层 import → 永远在最顶层
- `runtime` 是**唯一**允许同时持有 proxy / recorder / reporter / store / adapters 句柄的层（编排）
- `proxy` 不直接 import `reporter` / `store`——它只把记录交给 `recorder`（由 `runtime` 编排上报与存储）

## 4. 各层职责

### Layer 1 — `core`（最底层，零依赖）

**职责**：纯数据模型、配置 schema、通用工具，**无任何 IO**。

**对外暴露**：

```python
# 数据模型
@dataclass
class Record: ...               # 归一化记录
@dataclass
class DetectionResult: ...      # 检测结果
@dataclass
class EndpointSpec: ...         # 单个服务的配置（name, port, upstream, endpoint_type, ...）
@dataclass
class DetectorConfig: ...       # 检测服务器配置（url, api_key, ...）
@dataclass
class AppConfig: ...            # 完整应用配置 = DetectorConfig + List[EndpointSpec]

# 工具
def now_iso8601() -> str: ...
def redact_headers(h: dict) -> dict: ...   # 脱敏（默认允许在 core 里实现，因为是纯函数）

# 路径解析（依赖 platformdirs 第三方库；纯函数无 IO）
def resolve_config_dir() -> Path: ...        # 平台用户目录（Linux/macOS/Windows）
def resolve_config_path() -> Path: ...       # config.json 完整路径
def resolve_data_dir() -> Path: ...          # 数据目录（records / db / logs），跟随 config_dir
def ensure_dirs() -> None: ...               # 首次 init 时创建子目录，权限 0o700

# 配置校验（启动加载 config.json 时调用）
def validate_config(config: AppConfig) -> list[ConfigError]: ...

# 配置错误类别（对应 CLI 退出码 1）
class ConfigErrorCode(str, Enum):
    CONFIG_PARSE_ERROR = "CONFIG_PARSE_ERROR"             # JSON 格式损坏
    CONFIG_VALIDATION_ERROR = "CONFIG_VALIDATION_ERROR"   # 字段值不合法（如 port 冲突、URL 非 http(s))
    CONFIG_MISSING_FIELD = "CONFIG_MISSING_FIELD"         # 必填字段缺失（如 api_key 为空）
```

**配置校验规则**：

- `validate_config` 返回错误列表（**不抛异常**），由调用方决定如何呈现
- 校验项：
  - `detector.url` 是合法 HTTP(S) URL
  - `detector.api_key` 非空（X-API-Key 必填）
  - 各 service 的 `port` 在 [1, 65535]、**全局唯一**
  - 各 service 的 `upstream` 是合法 HTTP(S) URL
  - 各 service 的 `endpoint_type` 在三个枚举值内
  - `report_interval_sec > 0`、`batch_size > 0`、`max_queue_size > 0`
- 任何错误都映射到 CLI 退出码 `1`（用户错误）

**禁止**：HTTP、文件 IO、数据库、asyncio。

**为什么零依赖**：可以单独 `pip install` 后被任意脚本 / 测试 import，是整套系统的"领域字典"。

---

### Layer 2 — `recorder` / `reporter` / `store`（独立底层 IO）

**职责**：分别负责"记录"、"上报"、"存储"三件不同的 IO 事务。每层只对自己的 IO 负责，**不互相调用**。

**`recorder`**：
- 接收归一化记录（来自 `proxy`）
- 脱敏（基于 `core` 的 `redact_headers`）
- 写内存队列 + 异步追加 JSONL 落盘
- 提供同步 `enqueue(record)` 和异步 `flush()` 接口
- 内存队列**有上限**（`max_queue_size`，默认 10000），溢出时丢弃最旧记录并告警

```python
class Recorder:
    def __init__(
        self,
        queue_path: Path,                   # JSONL 落盘目录
        batch_size: int = 100,              # flush 单次返回的记录上限
        max_queue_size: int = 10000,        # 内存队列上限，超过则丢弃最旧
    ): ...
    def enqueue(self, record: Record) -> None: ...   # 同步，push 到内存队列
    async def flush(self) -> list[Record]: ...       # 异步，从**内存队列**取一拨出（不读 JSONL）
    async def aclose(self) -> None: ...              # 优雅关闭：等待内存队列 + 落盘 flush
    def queue_depth(self) -> int: ...                # 当前内存队列深度（给 status 用）
```

**关键语义**：

- `enqueue` / `flush` **互斥**，由 `asyncio.Lock` 保护临界区，禁止并发执行。
- JSONL 是**崩溃恢复的源**：进程崩溃 → 重启后从 `report_cursor` 之后读 JSONL 重放。正常运行时不依赖 JSONL 取批。
- 内存队列上限防 OOM：检测服务器长时间宕机 + JSONL 磁盘满时仍能稳态运行，可能丢数据但不崩。

**`reporter`**：
- 接收一批记录（来自 `runtime`）
- 构造 HTTP POST 请求（带 `X-API-Key`）
- 同步返回检测结果，含重试与超时
- **不**持久化任何东西

```python
class Reporter:
    def __init__(self, config: DetectorConfig, client: aiohttp.ClientSession): ...
    async def report(self, batch: list[Record]) -> list[DetectionResult]: ...
```

**`store`**：
- 接收检测结果（来自 `runtime`）
- 持久化到 SQLite
- 提供查询接口
- 启用 **WAL 模式** + `busy_timeout`，避免多写一读时 `database is locked`

```python
class Store:
    def __init__(
        self,
        db_path: Path,
        busy_timeout_ms: int = 5000,    # 锁等待超时
    ): ...
    async def save_results(self, results: list[DetectionResult]) -> None: ...
    async def query(self, since: datetime, service: str | None = None, limit: int = 100) -> list[DetectionResult]: ...
    async def get_cursor(self) -> ReportCursor: ...
    async def advance_cursor(self, cursor: ReportCursor) -> None: ...
```

**`Reporter` 错误分类**：

```python
class Reporter:
    def __init__(self, config: DetectorConfig, client: aiohttp.ClientSession): ...
    async def report(self, batch: list[Record]) -> list[DetectionResult]: ...
    # 区分错误类型，便于 runtime 决策：
    #   ReportErrorKind.AUTH        401/403（X-API-Key 失效）→ 停止重试，status 显示 "auth_failed"
    #   ReportErrorKind.PAYLOAD     4xx 其他（请求体问题）→  继续重试
    #   ReportErrorKind.SERVER      5xx / 网络错误 →         继续重试 + 指数退避
```

**依赖**：仅依赖 `core`。

**互相独立**：`recorder` 不知道 `reporter`；`reporter` 不知道 `store`；`store` 不知道 `recorder`。三者的协作由 `runtime` 编排。

---

### Layer 3 — `adapters`（协议适配层）

**职责**：理解三种 LLM 协议（OpenAI Chat Completions / OpenAI Responses / Anthropic Messages），把 HTTP 层透传的字节流解析成**结构化记录**。

**对外暴露**：

```python
class Adapter(ABC):
    endpoint_type: str

    @abstractmethod
    def parse_request(self, body: bytes) -> dict: ...

    @abstractmethod
    def on_stream_chunk(self, chunk: bytes) -> None: ...
    # ↑ 永不抛异常。坏数据进入 → 内部累计 error_chunk_count；partial 记录出来。

    @abstractmethod
    def finalize(self) -> dict: ...
    # ↑ 返回 {content, finish_reason, usage, raw}
    # ↑ 自身异常时由 proxy 兜底：content=raw_buffer, usage=None

    @abstractmethod
    def is_terminal(self) -> bool: ...
    # ↑ 终止标记检测（区分上游完成 vs 流中断）

class OpenAIChatCompletionsAdapter(Adapter): ...
class OpenAIResponsesAdapter(Adapter): ...
class AnthropicMessagesAdapter(Adapter): ...

def get_adapter(endpoint_type: str) -> Adapter: ...   # 工厂
```

**鲁棒性契约**（P1-12 流式异常处理）：

- `on_stream_chunk`：**绝不抛异常**。坏 JSON / 缺 `data:` 前缀 / 解析失败 → 内部累计 `error_chunk_count` 跳过，**不中断**透传。
- `finalize`：**必须被调用一次**（即使上游中断）。最终 `Record.error` 字段记录流量完整性（`null` = 完整流 / `stream_incomplete` / `upstream_timeout`）。
- adapter 是**纯函数式 + 状态对象**，无 IO，可独立测试。三种协议各需一组真实流式样例作为 fixture。

**依赖**：仅依赖 `core`（数据模型）。

**特点**：**无 IO**。纯函数式 + 状态对象。给定输入字节，产出结构化结果。独立可测试。

---

### Layer 4 — `proxy`（反向代理核心）

**职责**：起一个 HTTP 服务器，把请求转发到上游、边透传边累积、调用 `Adapter` 重组、最终把归一化记录喂给 `recorder`。**不知道**上报、存储、CLI 的存在。

**对外暴露**：

```python
class ProxyService:
    def __init__(self, spec: EndpointSpec, adapter: Adapter, recorder: Recorder, http_client: aiohttp.ClientSession): ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def status(self) -> dict: ...   # 给 runtime 用的状态
```

**依赖**：`core` + `adapters` + `recorder`（拿到 recorder 句柄）。

**关键边界**：`proxy` 调用 `recorder.enqueue(record)` —— 这是它能"对外"的最高层接口。**它不调用 reporter、store、runtime**。

---

### Layer 5 — `runtime`（运行时编排）

**职责**：唯一的编排者。持有所有层实例；负责启动、停止、定时任务、状态汇总。

**对外暴露**：

```python
class Runtime:
    def __init__(self, config: AppConfig, sources: ConfigSources): ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def status(self) -> dict: ...
    async def query_results(self, since: datetime, service: str | None = None) -> list[DetectionResult]: ...
    @staticmethod
    def build_from(config_path: Path = Path("./config.json"), **cli_overrides) -> Runtime: ...
    # ↑ 关键工厂方法：执行配置三级加载 + 校验 + 构造 Runtime
```

**配置三级加载**（`Runtime.build_from()` 内部）：

```python
# 伪代码（实现在 core/config.py，Runtime 仅调用）
config = load_config_json(path)              # 1. 配置文件
config = apply_env_overrides(config)         # 2. 环境变量
config = apply_cli_overrides(config, **cli_overrides)  # 3. 命令行
errors = validate_config(config)             # 4. 校验
if errors:
    raise ConfigValidationError(errors)
sources = ConfigSources.from_env_and_cli()   # 记录每个字段来源
return Runtime(config, sources)
```

**依赖**：所有下层（`core` + `recorder` + `reporter` + `store` + `adapters` + `proxy`）。

**编排流程**：

```
1. 加载 AppConfig
2. 构造 Recorder / Reporter / Store 实例
3. 为每个 EndpointSpec 构造 Adapter + ProxyService
4. 启动所有 ProxyService
5. 启动后台任务：
   - 每 report_interval_sec：recorder.flush() → reporter.report() → store.save_results()
6. 接收停止信号：优雅关闭（停止 ProxyService、取消后台任务、关闭 Recorder）
```

**为什么由 runtime 编排**：proxy / recorder / reporter / store 互不依赖，**只有 runtime 能看见全局**。这避免了"proxy 知道 reporter 的存在"这种隐性耦合。

---

### Layer 6 — `cli`（最顶层）

**职责**：解析命令行、调用 `runtime` 的接口、按"双形态输出契约"输出结果。

**对外暴露**：

```python
# 用 typer 实现
app = typer.Typer()

# 配置类（3 个）
@app.command()
def init(): ...

@app.command()
def validate(): ...

@app.command()
def config(ctx: typer.Context): ...     # 子命令：get / set / unset / list

# 生命周期类（5 个）
@app.command()
def start(): ...

@app.command()
def stop(): ...

@app.command()
def restart(): ...

@app.command()
def status(): ...

@app.command()
def logs(): ...

# 运维类（3 个）
@app.command()
def report(): ...

@app.command()
def redo(record_id: str): ...

@app.command()
def purge(): ...

# 调试类（2 个）
@app.command()
def doctor(): ...

@app.command()
def tail(): ...

# 所有命令全局支持：
#   --json         切换为机器可读 JSON 输出
#   --config PATH  指定配置文件路径（默认 ./config.json）
```

**命令与 runtime 方法的映射**：

| 命令 | 入口 | 后端调用 |
|---|---|---|
| `init` | 交互式 / `--api-key --detector-url` | 写 `config.json`（不走 runtime） |
| `validate` | 校验 | `core.validate_config()` |
| `config get <k>` | 单字段查询 | `core.get_field(config, key)` |
| `config set <k> <v>` | 修改+校验+快照 | `core.set_field(config, key, value)` + 写盘 |
| `config unset <k>` | 清除字段 | `core.unset_field(config, key)` + 写盘 |
| `config list` | 全字段+来源 | `core.collect_sources()` |
| `start` | 异步启动 | `Runtime.build_from(...).start()` + PID 文件 |
| `stop` | 优雅停止 | PID 文件 → SIGTERM → SIGKILL |
| `restart` | stop + start | 组合 |
| `status` | 内存状态 | `runtime.status()` |
| `logs` | 日志查看 | 读取日志文件（`--tail N` / `--follow`） |
| `report` | 查询检测结果 | `store.query()` |
| `redo <id>` | 重报 | `reporter.report([record])` + `store.save_results()` |
| `purge` | 清理过期 | 删除 `retention_days` 之前的 JSONL + SQLite 记录 |
| `doctor` | 自检 | 端口 / API / 磁盘 / SQLite / JSONL 多项验证 |
| `tail` | 实时跟踪 | 监听 JSONL append 事件流 |

**依赖**：仅 `runtime`（间接通过 `core` 拿配置）。

**特点**：薄壳层。**所有业务逻辑都在 `runtime` 及其下层**。`cli` 替换为 GUI / TUI / HTTP API 都不影响业务。

#### 输出契约（Agent 友好）

CLI 不是只给人看的，**必须支持 Agent 解析**。所有命令遵循三条约：

**1. 双形态输出**

- 默认：人类可读（表格、彩色、清晰列对齐）
- `--json` 标志：切换为 JSON 输出，Agent 可直接解析

```bash
# 人类可读
safe-guard status
#   SERVICE                  PORT  UPSTREAM               STATUS   QUEUE_DEPTH
#   openai-chat-completions  9001  https://api.openai.com running 12
#   openai-responses         9002  https://api.openai.com running 0
#   anthropic-messages       9003  https://api.anthropic  running 5

# 机器可读
safe-guard status --json
# {
#   "services": [
#     {"name": "openai-chat-completions", "port": 9001, "upstream": "https://api.openai.com", "status": "running", "queue_depth": 12},
#     ...
#   ],
#   "timestamp": "2026-08-14T..."
# }
```

**2. 退出码语义化**

| 退出码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 用户错误（参数错、配置不存在） |
| 2 | 运行时错误（端口被占用、启动失败） |
| 3 | 检测服务器错误（不可达、4xx/5xx） |
| 4 | 内部错误（未捕获异常） |

Agent 用 `$?` 即可判定下一步动作。

**3. `stdout` / `stderr` 分离**

- 数据 → `stdout`（Agent 重定向 `2>/dev/null` 即可拿干净数据）
- 日志 / 错误 → `stderr`（人类直接看错误流）

**错误输出格式（`--json` 形态下）**：

```json
{
  "ok": false,
  "error": {
    "code": "PORT_IN_USE",
    "message": "port 9001 is already in use",
    "detail": { "port": 9001, "holder_pid": 1234 }
  }
}
```

**非交互模式（`init` 等命令的 Agent 友好入口）**

`init` 默认交互式（人类方便），但支持全非交互参数：

```bash
safe-guard init --api-key "$API_KEY" --detector-url "http://detector:8080" --config ./config.json
```

**异步命令（`start`）**

`start` 是异步的（启动后立即返回，但服务在后台运行）。Agent 关心"启动是否成功"：

- `start` 立即退出（成功 `exit 0`，PID 文件已写入；失败 `exit 2/4`）
- Agent 用 `status --json` 查询运行状态
- 用 PID 文件做唯一性保证（避免多实例）

---

## 5. 依赖图（ASCII）

```
┌────────────────┐
│      cli       │  layer 7
└───────┬────────┘
        ▼
┌────────────────┐
│    runtime     │  layer 6
└────┬──┬──┬──┬──┘
     │  │  │  │
     ▼  ▼  ▼  ▼
┌────────────────┐  ┌──────────┐  ┌────────┐  ┌──────────┐
│    proxy       │  │ recorders│  │ reporter│  │ store    │  layer 1-3
└───────┬────────┘  └────┬─────┘  └────┬───┘  └────┬─────┘
        │                │             │            │
        ▼                ▼             ▼            ▼
┌────────────────┐  ┌─────────────────────────────────────┐
│   adapters     │  │              core                   │  layer 0
└───────┬────────┘  └──────────────────┬──────────────────┘
        │                             │
        └─────────────┬───────────────┘
                      ▼
                   (core)
```

**箭头方向**：依赖（A → B 表示 A import B）。**不存在反向箭头。**

## 6. 依赖规则与禁止

| 规则 | 说明 |
|---|---|
| R1 | 层编号小的**不得** import 层编号大的 |
| R2 | 同一层内部目录互不引用（避免同层循环） |
| R3 | `proxy` 只允许 import `core` / `adapters` / `recorder` |
| R4 | `runtime` 是唯一允许导入多个下层的层（编排需要） |
| R5 | `core` 不得 import 任何业务层（只能 import 标准库） |
| R6 | `cli` 不得被任何非 `cli` 文件 import |
| R7 | 任何层都不得 import 第三方 HTTP 框架、数据库驱动在 `core` 中 |

**检查手段**（实现阶段）：用 `pydeps` / `importlinter` 跑依赖图，作为 CI 检查。

## 7. 关键接口约定

### 7.1 `Record`（由 `core` 定义，由 `adapters` 填充，由 `proxy` 提交到 `recorder`）

```python
@dataclass
class Record:
    record_id: str              # UUID，由 proxy 生成
    service: str                # endpoint name（来自配置）
    endpoint_type: str          # openai-chat-completions / openai-responses / anthropic-messages
    upstream: str               # 例如 https://api.openai.com
    path: str                   # 例如 /v1/chat/completions
    timestamp: str              # ISO8601
    elapsed_ms: int             # 端到端延迟
    status_code: int            # 上游响应状态
    error: str | None           # 失败原因（若有）
    request: dict               # 由适配器解析出的结构化请求
    response: dict              # 由适配器重组出的结构化响应
```

### 7.2 `proxy` → `recorder` 的契约

```python
# proxy 完成后调用一次：
recorder.enqueue(record)
```

这一行就是 proxy 与外界唯一的耦合点。recorder 怎么落盘、谁会来 flush，都是 proxy 不知道的事。

### 7.3 `runtime` 的后台任务接口

```python
async def _report_loop(self):
    # 1. 启动续传：若 SQLite 游标有 last_record_id，先从 JSONL 重放未上报的记录
    await self._replay_unreported()

    # 2. 周期上报循环
    backoff = 1
    while not self._stopped:
        await asyncio.sleep(self.config.detector.report_interval_sec)
        batch = await self.recorder.flush()
        if not batch:
            backoff = 1
            continue
        try:
            results = await self.reporter.report(batch)
            await self.store.save_results(results)
            await self.store.advance_cursor(last_of(batch))
            backoff = 1
        except ReportError as e:
            if e.kind == ReportErrorKind.AUTH:
                # API key 失效：停止重试，status 显示 "auth_failed"
                self._auth_failed = True
                log_error("X-API-Key 失效，已停止上报；请重新 init")
                return
            # 其他错误（5xx / 网络 / 4xx_payload）：指数退避
            await asyncio.sleep(min(60, 2 ** backoff))
            backoff = min(backoff + 1, 6)

async def _replay_unreported(self):
    cursor = await self.store.get_cursor()
    for record in iter_records_since(cursor):
        try:
            results = await self.reporter.report([record])
            await self.store.save_results(results)
            await self.store.advance_cursor(record)
        except ReportError as e:
            log_warning(f"续传失败: {record.record_id}: {e}")
            break
```

**关键点**：

- 启动时先**续传**未上报记录（用 `iter_records_since(cursor)`），再进入周期循环。
- 错误分类：`AUTH` → 停止重试；其他 → 指数退避（base=2s，max=60s）。
- `auth_failed` 状态被 `status` 命令读取并展示。

## 8. 包结构（目录）

```
saitec-safe-guard/
├── pyproject.toml
├── README.md
├── docs/
│   └── design/
│       ├── saitec-safe-guard-cli-design.md
│       └── architecture.md            ← 本文档
├── src/
│   └── saitec/
│       ├── __init__.py
│       ├── core/                       # layer 0
│       │   ├── __init__.py
│       │   ├── models.py               # Record, DetectionResult, EndpointSpec, AppConfig
│       │   ├── config.py               # 配置 schema、加载
│       │   └── utils.py
│       ├── recorder/                   # layer 1
│       │   ├── __init__.py
│       │   ├── recorder.py
│       │   └── redactor.py
│       ├── reporter/                   # layer 1
│       │   ├── __init__.py
│       │   └── reporter.py
│       ├── store/                      # layer 1
│       │   ├── __init__.py
│       │   └── sqlite_store.py
│       ├── adapters/                   # layer 4
│       │   ├── __init__.py
│       │   ├── base.py                 # Adapter 抽象基类
│       │   ├── openai_chat_completions.py
│       │   ├── openai_responses.py
│       │   └── anthropic_messages.py
│       ├── proxy/                      # layer 3
│       │   ├── __init__.py
│       │   ├── server.py               # ProxyService
│       │   └── streaming.py            # 流式透传工具
│       ├── runtime/                    # layer 6
│       │   ├── __init__.py
│       │   └── runtime.py
│       └── cli/                        # layer 7
│           ├── __init__.py
│           ├── main.py                 # typer 入口
│           └── commands/
│               ├── init.py
│               ├── start.py
│               ├── status.py
│               └── report.py
└── tests/
    ├── test_core/
    ├── test_recorder/
    ├── test_reporter/
    ├── test_store/
    ├── test_adapters/
    ├── test_proxy/
    ├── test_runtime/
    └── test_cli/
```

注意：

- 每个目录一个 `__init__.py`，**只 export 对外接口**（不暴露内部细节）
- `tests/` 镜像源码结构，每个层独立测试
- `src/` 布局（而不是平铺）能强制任何 import 必须经包名，避免"绕过层级直接 import 内部文件"

## 9. 跨层数据流（一次请求）

```
[客户端]                                                 layer 7 cli
   │ (command: start)
   ▼
[cli] ── start() ──▶ runtime.start()                       layer 6
                              │
                              ▼
              [runtime] ── new ProxyService(...) ──▶ proxy  layer 3
                              │
                              ▼
              [proxy] ── aiohttp.web.Application.listen() ──▶ 监听 9001
                              │
                              ▼
[客户端 SDK] ── http → 127.0.0.1:9001
                              │
                              ▼
              [proxy] ── adapter.parse_request(body) ──▶ adapters producing dict
                              │
                              ▼
              [proxy] ── aiohttp 转发到 upstream
                              │
                              ▼
              [proxy] ── adapter.on_stream_chunk(chunk) ──▶ 累积
                              │
                              ▼
              [proxy] ── adapter.finalize() ──▶ {content, usage, ...}
                              │
                              ▼
              [proxy] ── recorder.enqueue(Record) ──▶      layer 1 (recorder)
                              │
                              ▼  ── 异步 ──▶ JSONL 落盘
                              │
                              ▼
              [runtime] ── 每隔 report_interval_sec flush + report + save
                              │
                              ▼
              SQLite 检测结果 ──▶ cli.report 查询           layer 7
```

**只有一个数据归属**：原始 HTTP 字节从 `proxy` 流过，被 `adapters` 解释成 `Record`，被 `recorder` 吸收，再被 `runtime` 编排上报。**任何一层都不持有跨层数据**。

## 10. 测试策略

每层独立可测试，且**不需要上层**：

| 层 | 测试策略 | 关键 fixture |
|---|---|---|
| `core` | 纯单元测试，无 IO | 无 |
| `recorder` | 测试内存队列、落盘、重启恢复 | tmp_path |
| `reporter` | `aiohttp` mock server 模拟检测服务器 | aiohttp test server |
| `store` | 临时 SQLite 文件 | tmp_path |
| `adapters` | 三种协议各一组**真实结构样例**（流式 + 非流式） | 真实流量样本 fixture |
| `proxy` | 跑一个本地 HTTP server 模拟上游 + 内存 recorder | Mock upstream |
| `runtime` | 集成测试：端到端跑一次虚拟请求 | 所有下层实例 |
| `cli` | `typer.testing.CliRunner` | CliRunner 注入 memory runtime |

**测试分层原则**：

- **单元**：单层 + 假的下层接口
- **集成**：相邻两层协作（如 `adapter + proxy`）
- **端到端**：从 `cli` 触发的完整流程（仅 1-2 个核心场景）

## 11. 演进与替代

这层架构的最大价值是"可替换"。常见的演进路径：

| 演进 | 替换哪层 | 影响 |
|---|---|---|
| 接入新厂商 LLM | 新增 `adapters/xxx.py` | 不影响其他层 |
| 改用 gRPC 检测服务器 | 替换 `reporter/` | recorder / store / runtime 仅需小幅调整 |
| 改用 PostgreSQL 存结果 | 替换 `store/` | recorder / proxy / adapters 都不动 |
| 提供 REST API 接口（替代 CLI） | 新增 `cli2/` 或 `api/`，复用 `runtime` | 业务层完全不动 |
| 改用 Rust 重写 proxy | 替换 `proxy/`，沿用 `core` 数据模型 | recorder / runtime 通过接口契约兼容 |

**关键约束**：

- 接口一旦稳定，**下游**可以独立演进，但**上游不能假设下游内部实现**
- 反之亦然：proxy 不能假设 recorder 内部是 JSONL 还是 SQLite

---

## 12. 与总体设计文档的对应关系

| 总体设计章节 | 架构对应 |
|---|---|
| §3 关键技术决策 | 全部沉淀在本架构 |
| §5 架构设计 | 本文 §3 + §5 |
| §6 协议适配层 | 第 4 节 Layer 3 `adapters` |
| §7 数据流 | 本文 §9 |
| §8 流式响应（边透传边累积） | 本文 §4 `proxy` + `adapters` 协作 |
| §9 错误处理 | 本文 §7.3 runtime + proxy 错误传播 |
| §10 敏感信息脱敏 | 本文 §4 `core` 中的 `redact_headers` + `recorder` 应用 |
| §12 配置设计 | 本文 §4 `core` AppConfig 落地 |
| §13 CLI 命令 | 本文 §4 Layer 6 `cli` |

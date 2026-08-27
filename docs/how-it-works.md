# 实现原理导览（how-it-works）

> 面向想了解"这个 CLI 内部到底怎么运作"的读者。用通俗语言讲清楚每一块的设计，
> 读完你应该能回答：一个请求从进来到结论入库经历了什么、每个目录在干什么、
> 为什么有些地方是这样设计的。
> 更正式的规格见 `docs/design/` 三件套，本文不重复字段级细节。

---

## 1. 一图看懂

```
你的客户端                ssgc（本机）                          外部
─────────              ────────────────────                    ────
Claude Code ──请求──→ ① 本地代理端口(9001..)
                        │  透明转发（不动内容）
                        │                     ──原请求──→  真实上游
                        │                     ←──响应────  (DeepSeek等)
                        │  ←──响应原样回给客户端
                        ↓
                     ② 边转发边"抄写"：拼出一份归一化记录(Record)
                        │
                     ③ 内存队列 → 周期落盘 JSONL（先落盘，防丢）
                        │
                     ④ 每个上报周期(默认60s)：取一批 ←──────── 检测服务器
                        │   POST 过去（X-API-Key 鉴权）        （你们实现）
                        │   拿回每条记录的检测结论
                        ↓
                     ⑤ 结论写本地 SQLite（report 命令查的就是它）
```

记住这条主线，下面所有内容都是它的展开。

---

## 2. 六层架构：谁在干什么

代码在 `src/ssgc/` 下分六层，**依赖严格单向：上层只能 import 下层**（cli → runtime → proxy/adapters → recorder·reporter·store → core）。这条铁律让改动的影响范围可预测。

| 层 | 目录 | 一句话职责 | 通俗理解 |
|---|---|---|---|
| L1 | `core/` | 数据模型、配置加载校验、路径、纯工具函数 | 最低层的"词汇表"，谁都能用，它不用任何人 |
| L2 | `recorder/` `reporter/` `store/` | 三个独立的存储/网络小件：JSONL 落盘、HTTP 上报、SQLite 读写 | 三个互不相识的工人，各自只干一件事 |
| L3 | `adapters/` | 三种协议（OpenAI Chat/Responses、Anthropic）的"翻译官"：把不同格式的请求/响应解析成统一的 Record | 让上层不用关心"这是 OpenAI 还是 Anthropic 的报文" |
| L4 | `proxy/` | 反向代理本体：收请求→转发→透传响应，边透传边喂给 adapter | 整个工具的"前台"，但故意做得极薄 |
| L5 | `runtime/` | **唯一编排者**：把上面所有件拼起来，跑后台循环（落盘、上报、续传） | 乐队的指挥，所有件由它创建和注入 |
| L6 | `cli/` | 命令行入口：15 个命令，解析参数→调 runtime/store→输出 | 面子，不含业务逻辑 |

**为什么这样分？** 两句话：① 每层可以被单独测试（下层不知道上层的存在）；② 想加新协议只动 L3，想加新命令只动 L6，互相不传染。

**层间怎么通信？** 只通过构造函数传对象（依赖注入），没有全局变量、没有模块级互相 import。比如 `Runtime` 自己创建 Recorder/Reporter/Store 然后把 Recorder 塞给 ProxyService。

---

## 3. 一个请求的完整一生

以 Claude Code 发一条消息（经 `http://127.0.0.1:9001/v1/chat/completions`）为例：

**第 1 步：进代理（`proxy/server.py`）**
代理在本机 9001 端口起了个 aiohttp 服务器，catch-all 路由——任何路径任何方法都接。
真实转发地址 = `upstream + 客户端请求的原始路径`（纯前缀拼接，不改写路径）。
请求头里只剥掉 `Host`/`Content-Length`/`Transfer-Encoding`（这三个描述"这一跳"的连接，透传无意义），其余原样带上去——包括你客户端的 `Authorization`，所以 ssgc 自己不做任何鉴权。

**第 2 步：转发上游，拿到响应**
分两条路：
- **非流式**：把上游响应体整个读进内存（有 100MB 上限防 OOM）
- **流式（SSE）**：一边收到 chunk 一边原样写给客户端（你看到的打字机效果不受影响），同时把 chunk 累积喂给 adapter

**第 3 步：翻译成 Record（`adapters/`）**
adapter 把原始报文"翻译"成统一结构：request 侧提取 `model/messages/tools/stream`；response 侧把 SSE 碎片拼成完整回答文本 + usage。
SSE 解析最麻烦的坑：TCP 分包可能把一行 `data: {...}` 劈成两个 chunk，adapter 里有行缓冲做半行拼接。
（gzip 也是在这层附近处理的：aiohttp 自动把上游的 gzip 响应解压成明文，所以代理回传时必须把 `Content-Encoding` 头剥掉，否则客户端会按 gzip 去解明文而报错——这是 8/26 修过的真实 bug。）

**第 4 步：记录进队列（`recorder/recorder.py`）**
Record 塞进内存队列（有上限，满了丢最旧的，审计优先保活）。此时**还没落盘**。

**第 5 步：周期落盘 JSONL**
runtime 的循环每隔几秒调 `recorder.flush()`：从队列取一批（≤batch_size）→ **先写进按天分片的 JSONL 文件** → 再把这一批交给上报。
注意顺序：**先落盘后上报**。这保证哪怕下一秒断电，记录也在磁盘上（JSONL 是崩溃恢复的依据，SQLite 只是结论缓存）。

**第 6 步：上报（`reporter/reporter.py`）**
每个上报周期（默认 60s）把一批 Record `POST {detector.url}{detector.endpoint_path}`，带 `X-API-Key`。
服务端返回每条记录的结论（`results` 数组，靠 `record_id` 关联回来）。

**第 7 步：结论入库（`store/store.py`）**
结论 + Record 的元信息写进 `~/.ssgc/results.db` 的 `detection_results` 表（对 `record_id` 做 UPSERT，重复上报安全）。
你跑 `ssgc report` 查的就是这张表。

至此一个请求走完，通常在第 5 步后 0~60 秒内完成全部落库。

---

## 4. 五个关键机制

### 4.1 配置三级覆盖

同一个配置项可以从三个地方给值，优先级 **命令行参数 > 环境变量 > config.json**。

- `config.json`（`~/.ssgc/config.json`）是基底，`init` / `config set` / `service add` 写的都是它
- 环境变量族 `SSGC_*`（如 `SSGC_REPORT_INTERVAL`）临时盖一层，不落盘
- 命令行参数（如 `ssgc start --report-interval 5`）最高优先

`start` 的临时参数实现上就是转成 `SSGC_*` 环境变量传给后台子进程（`start.py` 里可见）。

### 4.2 进程模型：start / stop 是怎么工作的

`ssgc start` **不是**让当前进程变成服务，而是：
1. 用 `subprocess.Popen` 拉起一个独立 Python 子进程（`_serve.py`，Windows 下 `CREATE_NO_WINDOW` 无窗口）
2. 子进程的 PID 写进 `~/.ssgc/ssgc.pid`
3. 父进程立刻退出——所以你终端关了服务照样跑

`ssgc stop`：读 PID 文件 → 写一个 `ssgc.stop.flag` 文件 → 子进程主循环轮询到 flag 就优雅收尾（最后 flush + 上报）→ 超时（默认 10s）则强杀。
Windows 没有 SIGTERM 优雅信号，所以用 flag 文件轮询这个朴素办法——跨平台且可靠。

`status` / `doctor` 都是拿 PID 文件从外部"探望"：查进程在不在（探活）、端口通不通。status 掉线时 exit code 仍是 0——**判断健康要看 `data.running` 字段**，这是踩过的坑。

### 4.3 上报循环：游标、续传、退避

`runtime._report_loop` 是核心后台循环，伪代码：

```
启动时：_replay_unreported()   # 续传：读 JSONL，把游标之后的旧记录补报
循环每 60s（或退避间隔）:
    batch = recorder.flush()          # 拿一批已落盘的
    resp = POST 给 detector
    成功 → 结论入库 + 游标推进（记录"报到最后一条是哪个"）
    失败 → 按错误类型处置（见下）
```

**游标**（`report_cursor` 表，单行）记录最后成功上报的 record_id + 时间戳。作用：进程崩溃/重启后，从 JSONL 里把游标之后没报过的记录重新报出去——**落盘先于上报 + 游标 + JSONL 重放 = 数据不丢**。

**错误三分类**（detector 返回什么决定 ssgc 的行为）：

| detector 返回 | 分类 | ssgc 行为 |
|---|---|---|
| 200 | 成功 | 游标推进，结论入库 |
| 401 / 403 | AUTH | **停止上报循环**（避免无限重试锁死），日志提示重配 api_key |
| 其他 4xx | PAYLOAD | 记录保留重试（可能是报文不兼容） |
| 5xx / 网络错误 | SERVER | 指数退避重试（2s→4s→…→上限 60s），恢复后自动补报 |

**AUTH 停摆是有意设计**：key 错了重试一万次也没用，不如停下让你修。修好 restart，续传机制会把积压的都补上。

### 4.4 monitor：不是 tail 日志

`ssgc monitor`（前台实时监控）容易误解为"盯着日志文件看"，实际是**进程内事件流**：Runtime/ProxyService 暴露了 `event_sink` 钩子参数（一个回调函数），关键节点（流量进来、上报完成、violation 命中、上报失败）都会调它发结构化事件。monitor 命令就是传入一个"把事件打印成彩色单行"的回调。

为什么不用别的方案？——拦截响应看不到 violation（结论在 detector 侧异步产生，响应里没有）；tail 日志是非结构化轮询。事件流既实时又结构化。
这也是为什么 monitor 与 start 互斥：事件回调在进程内存里，monitor 必须**自己**是那个服务进程。

### 4.5 数据的双轨制：JSONL vs SQLite

| | JSONL（`~/.ssgc/records/records-YYYY-MM-DD.jsonl`） | SQLite（`~/.ssgc/results.db`） |
|---|---|---|
| 存什么 | **原始完整记录**（每条 Record 一行 JSON） | **检测结论 + 元信息** |
| 角色 | 事实源 / 崩溃恢复依据 / redo 的数据来源 | 查询缓存（report 命令的索引视图） |
| 可再生？ | 不可（丢了就没了） | 可（从 JSONL redo 重建结论） |

所以 `purge` 清理时对两者态度不同，`redo` 命令能重报历史记录（数据源就是 JSONL）。

`detection_results` 表除了结论字段（`detection_status/risk_level/detection_detail/detected_at`），还冗余存了 Record 的关键元信息（model、token 用量、状态码、耗时、请求/响应摘录）——为了 report 查询不用回 JSONL 找。`detection_detail` 是 detector 返回的自由 JSON，原样透传。

---

## 5. 目录速查

```
src/ssgc/
├── core/       models.py(Record等数据类)  config.py(加载+三级覆盖)  paths.py(~/.ssgc解析)
├── recorder/   recorder.py               内存队列→JSONL 批量落盘
├── reporter/   reporter.py               HTTP 上报 + 错误三分类
├── store/      store.py                  SQLite WAL(detection_results+report_cursor)
├── adapters/   base.py(抽象) + openai_chat_completions/openai_responses/anthropic_messages.py
├── proxy/      server.py                 ProxyService：catch-all 转发 + SSE 透传
├── runtime/    runtime.py                编排一切：start/stop/上报循环/续传/event_sink
└── cli/        main.py(typer入口+编码修正) _common.py(输出/视觉/路径) _serve.py(后台子进程入口)
                commands/  15 个命令一文件一命令
tests/          镜像 src 分层（test_proxy 对应 proxy 层……）+ mock_detector/(联调桩) + test_chat/(发真实流量的手动脚本)
docs/           design/(正式规格) integration/detector-api.md(对接契约) issues/(历史问题清单)
```

加新东西往哪写：新协议→`adapters/` 新文件；新命令→`cli/commands/` 新文件；新配置字段→core/models + core/config + config_cmd + init **四处同步**（漏一处就是 bug，详见 AGENTS.md §3 的决策表）。

---

## 6. 设计取舍（为什么"不做"某些事）

- **不阻断流量**：检测是事后审计。要阻断就得等结论再放行，LLM 响应几秒就变几十秒，代理失去透明性。violation 的价值在审计与告警。
- **不做 MITM/证书**：靠"客户端自己把 base_url 指到本地"实现，省掉证书信任链的所有麻烦。代价是只适用于你能改配置的客户端。
- **本机明文 http**：流量不出本机回环，没有加密需求；真实上游该 https 还是 https（代理透传）。
- **单用户单进程串行上报**：不做并发上报——检测服务器对接方实现简单（无并发竞争），量级也用不着。
- **--json 是 Agent 契约**：字段只增不减、语义不变。人类可读输出（颜色/emoji/表格）随便美化，互不影响。

---

## 7. 测试怎么组织的

250 个 pytest 全在 `tests/`，目录镜像 src 分层。三层测试观：

1. **单元/集成测试**（pytest 自动跑）：mock 上游起真实 aiohttp 服务器验证转发；mock detector 是个真 FastAPI（`tests/mock_detector/`，随机或 LLM 判定两种模式）
2. **CLI 测试**（`test_cli/`）：typer 的 CliRunner 进程内调命令，不真起子进程
3. **端到端联调**（手动）：`tests/test_chat/chat_probe.py` 经代理发 12 条混合内容消息打真实模型

经验教训（都记在 PROGRESS.md）：单元测试全绿不等于 CLI 没问题——start 子进程秒崩、GBK 乱码、gzip 头透传这些全是真实跑命令才暴露的。所以修完东西要真跑一遍 CLI。

---

## 8. 常见问题速答

**Q：ssgc 会拖慢我的请求吗？**
非流式路径多一次内存拷贝（微秒级）；流式路径 chunk 直接透传不缓冲。瓶颈永远在上游模型本身。

**Q：检测服务器挂了会丢数据吗？**
不会。JSONL 先落盘；上报失败进退避重试；detector 恢复后游标+续传自动补报。唯一"停"的场景是 401/403（配置错），修好 restart 续传补上。

**Q：record_body 关掉会怎样？**
Record 只存元数据骨架不存对话内容。检测服务器只能看到"有这么一次请求"看不到内容——检测依赖内容的话必须开着（默认开）。

**Q：为什么 SQLite 里结论有四种值？**
clean/suspicious/violation/error。error 表示"检测本身失败"（detector 侧问题），不是流量有问题。

**Q：改了配置为什么不生效？**
配置在 start 时读一次，后台子进程不会感知变化——`ssgc restart`。

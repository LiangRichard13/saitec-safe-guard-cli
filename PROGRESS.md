# PROGRESS.md — 项目进度

> 倒序时间线。格式约定见 [AGENTS.md](AGENTS.md) §5。接手 Agent 先读本文件建立上下文。
> 条目与 commit 一一对应；`教训:` 行是防重踩的关键。

## 2026-08-26

### docs: 新增 how-it-works.md 实现原理通俗导览 <0cc1502>

### fix: banner ASCII art 字母顺序拼错 SGCS→SSGC（手拼顺序笔误） <61e70d7>

### fix: 复查修 4 处 ssgc-cli 误转产物（重命名遗漏） <0e08d98>
用户要求脚本+grep 双重复查：发现 sed 把 `safe-guard-cli` 误转 `ssgc-cli`——AGENTS.md 两处指向不存在的 skills/ssgc-cli/ 路径、SKILL.md frontmatter name、evals.json skill_name；另心跳脚本 docstring 5 处旧命令、worktree 目录残留。全部修正后脚本/grep/运行时 help 三层验证 0 残留。
教训: 全局替换的残留检查 pattern 必须包含"替换产物"（ssgc-cli）而不仅是旧名（safe-guard）——误转产物引用失效比旧名残留更危险；skill 目录改名时 frontmatter name 与 evals.json 的 skill_name 是独立引用点。

### test: service list 断言剥 ANSI（FORCE_COLOR 环境 flaky） <de296c3>
Claude Code/CI 会话注入 FORCE_COLOR 时 rich 对 CliRunner 非 TTY 输出也着色并把 URL 拆成多段 SGR，纯文本断言匹配不上。断言前统一 `_plain()` 剥 ANSI。
教训: 富文本 CLI 的输出断言一律先剥 ANSI 再比——环境注入的 FORCE_COLOR 会让"非 TTY 无色"的假设失效；FORCE_COLOR 的优先级高于 NO_COLOR。

### chore: 清理 platformdirs 文案残留 <f382658>
重命名提交漏改 help/docstring 里的 3 处 platformdirs 描述（依赖已删）。

### refactor: 品牌统一 SSGC——包名/命令/路径/文档全量重命名 <9b0d301>
命名体系定稿：正式名 `saitec-safe-guard-cli`（repo/PyPI 不变），代码内一律 `ssgc`（src 目录/import/CLI 命令/环境变量 SSGC_CONFIG），品牌 SSGC，数据目录 `~/.ssgc`（弃 platformdirs，删依赖），日志/PID/stop flag 改 `ssgc.*`，banner 换 SSGC，skill 目录改名 `.claude/skills/ssgc`。不保留旧名兼容（无 safe-guard 别名、不回退 SAITEC_CONFIG）。保留：conda 环境名 saitec-guard、GitHub URL、作者 SaITec、本文件历史条目。本机已完成：conda 重装（ssgc.exe 替换 safe-guard.exe）、数据迁移至 ~/.ssgc（复制→全链路验证→删旧目录）、心跳脚本 exe 路径同步。
教训: 全局替换 `safe-guard` 前必须先用占位符保护 `saitec-safe-guard-cli`（PyPI/repo 名是其超集，会误伤）；`patch("saitec.xxx")` 这类字符串模块路径不被 `from saitec`/`import saitec` 两条 sed 规则覆盖，残留检查必须独立跑。

### docs: 文档矩阵收紧——bug 修复不更新 user-guide/SKILL <208e5cf>
撤销 gzip 修复在 user-guide §7.9 / SKILL 排错树与陷阱表 / operations.md 的排错条目（issues 清单保留）。AGENTS.md §4 矩阵拆分"CLI 操作行为变化"与"bug 修复"两行并加原则注脚：已修复的 bug 用户不会再遇到，排错条目只收仍存在/需用户操作的问题。
教训: 排错文档是给"还会遇到这个问题的人"看的——修复提交里顺手写排错条目是把文档矩阵当成了"改动清单"而非"用户视角"。

### fix: P0 gzip 上游响应头透传导致客户端全量 Connection error <812ddeb>
DeepSeek（压缩上游）联调暴露：aiohttp `auto_decompress=True` 已解压 body，但 `Content-Encoding: gzip` 头被原样透传 → openai SDK 按 gzip 解码明文失败，报 Connection error 并重试 2 次（代理侧全 200、JSONL 同请求 ×3 是识别指纹）。修复：两处响应头过滤集合提取为 `_STRIP_RESPONSE_HEADERS` 常量并加 `content-encoding`；+2 回归测试（非流式/SSE）；236 pytest 全绿，真实 DeepSeek 端到端验证通过。
教训: 代理改写了 body（解压/重组/分块）就必须重算/剥离描述 body 表示的头（Content-Length/Transfer-Encoding/**Content-Encoding**）——之前只想到前两个；本地不压缩的上游测不出这类 bug，必须用真实压缩上游联调。

### feat: mock llm 模式同会话前缀去重（token O(N²)→O(N)） <a5044d1>
完整快照语义下单会话第 N 条 Record 含前 N-1 轮全部内容，LLM 逐条全量送审时总审读量 O(N²)。按 messages 算 sha256 hash 链做已审前缀匹配：整条命中复用结论、部分命中只送新增轮次 + "前情已审"上下文行（防漏检跨轮攻击）。契约不变（去重是 detector 内部优化）。真实 LLM 实测 10 轮会话节省 81.8%（50 轮理论 ~96%）。detector-api.md 增"实现建议"章节。
教训: 去重责任放 detector 侧而非 CLI 侧——只有有缓存的一方知道"审过什么"；CLI 判断"同会话"不可靠（客户端会编辑历史）。

### docs: README/user-guide 同步 monitor 命令与命令计数 <a6676fe>
教训: monitor 功能提交时同步了 SKILL/user-guide 但漏了 README 速查与命令计数——功能提交前必须按 AGENTS.md §4 文档矩阵逐项核对。

### feat: monitor 前台实时监控命令（人盯场景） <8d9efff>
监控点选型走进程内事件流（runtime/proxy 的 event_sink 钩子发结构化事件），非拦截响应（violation 结论在 detector 侧异步产生，响应里没有）也非 tail 日志（非结构化轮询）。与 start 双向互斥（共享 PID 文件），stop 命令可停，Ctrl+C 优雅退出。

### feat: safe-guard-cli 项目级 skill（教 Agent 操作本 CLI） <ef8cf2b>
`.claude/skills/safe-guard-cli/`（SKILL.md + references + evals）。skill-creator 流程评估：6 子代理 with/without 对照，断言通过率持平（baseline 靠读源码也能完成），效率优势显著（耗时 446s vs 1045s，-57%）。
教训: ①测试暴露 SKILL 两处事实错误（start 对死 PID 自动覆盖、status 掉线时 exit 仍 0——健康判断必须查 data.running 字段）；②评测的 with/without 并发子代理必须用独立配置目录，共目录会互相污染；③Windows 下 skill-creator 脚本要 `PYTHONUTF8=1`（GBK 读中文 JSON 崩）。

### feat: CLI 人类可读输出视觉升级（rich/emoji/banner） <60a3d15>
rich（typer 传递依赖，零新增安装）：start/restart 显示 SAFE GUARD figlet banner、doctor/report 表格、emoji 图标集。`--json` 契约零改动；非 TTY 自动无色（测试友好）。版本号从包元数据读（pyproject 单一来源）。
教训: pipe 下 GBK 无法编码 emoji/✓——main.py 的 `_configure_io_encoding`（pipe 强制 UTF-8）是 rich 可用的前提。

### feat: mock detector 支持 LLM 判定模式 <95d0b31>
`MOCK_DETECTION_MODE=random|llm`；llm 模式逐条调 OpenAI 兼容 API 判定，失败降级 error 结论不阻断。
教训: 用户在 `tests/mock_detector/.env` 配了 llm+key 后，mock 测试真调 LLM（单轮 244s 且断言失败）——test_mock 的 client fixture 必须强制 `DETECTION_MODE=random` 与用户本地 .env 解耦。

### feat: 日志按日期切割 + purge 清理日志 <86ca0de>
TimedRotatingFileHandler(午夜, backupCount=14)；purge 增加日志备份清理（活跃文件永不删）。

## 2026-08-21

### feat: 自定义监控端点配置体验重构（service 子命令组） <6f62476>
service add/remove/set/list + init 强制显式 --upstream（单服务起步，不再生成官方地址 3 服务模板）+ upstream 误配完整端点 URL 的防呆警告 + 服务映射统一输出。解决"默认配置写死官方地址、CLI 无法增删服务"的核心缺口。
教训: `_set_by_path` 按已存在 name 匹配数组——纯 config set 无法新增 service，需要专门的 service 命令组操作 services 数组。

### feat: detector endpoint_path 配置 + 服务端对接文档 <90b89db>
同一 IP:端口下不同检测接口路径（url 只含 scheme+host+port，路径放 endpoint_path）。

### docs: 用户手册（user-guide.md ~750 行）+ README 重写 <02e7d68>

### fix: 8 个 CLI 鲁棒性问题（真实使用发现） <94380f8>
P0 `_serve.py` 相对导入导致 start 子进程秒崩（改绝对导入）；doctor 端口检查语义错误（运行时 connect/未运行时 bind）；Windows GBK 编码乱码（`_configure_io_encoding`）；AUTH 错误信息透传；init 参数校验等。
教训: ①`_serve.py` 被 `python <path>` 方式调用时无父包，相对导入必炸——子进程入口一律绝对导入；②CLI 工具的测试不能只靠单元测试，必须真实跑命令行（本次 8 个问题全在实操中暴露，单测全绿）；③conda 环境的 exe 不在 PATH，全路径调用。

## 2026-08-20

### test: FastAPI mock 检测服务器 <4dcdedb>
随机 5% 标 violation + /records 查询；starlette TestClient 需要 httpx（加入 mock extras）。
教训: fastapi 与 starlette 版本必须匹配（fastapi 0.115 给 starlette 1.x 传已删除的 on_startup 直接崩）——环境级不兼容报错先查版本配对。

### test: 补齐 CLI 与 runtime 后台循环单元测试（+19，覆盖 71%→79%） <594d4b1>

## 2026-08-19

### fix: 12 个初版严重问题（3 维度 review 发现） <52cd7c4>
P0：SSE 跨 chunk 半行拼接（adapters 加行缓冲）、非流式响应解析（bare-JSON 快路径）、flush 先落盘后出队、上报失败 pending 保留、api_key 脱敏、数据目录跟随 config。P1：env 传递、exit 码、Windows stop.flag、响应体上限。
教训: SSE 流式解析必须处理 TCP 分包把一行劈两半的情况；dataclasses.replace 是浅拷贝。

### feat: 初版六阶段交付 <73089b5 及此前>
e6a3e8d 骨架 → 64f1798 core(三级覆盖) → 61fe087 store → 4af4449 recorder/reporter → 6d3f32c adapters(三协议) → fcc677c proxy(流式透传) → c5046be runtime(编排/上报循环/游标续传) → 73089b5 CLI(13 命令)。测试随阶段同步写（167 个）。

---

## 未完成 / 待办
- 真实检测服务器接口定型后：把 mock 的前缀去重经验合入其实现（见 detector-api.md 实现建议）
- 传输膨胀若成痛点：batch gzip（Content-Encoding）——见 detector-api.md 附注
- v2 可选：monitor attach 已运行实例（需 IPC）；CLI 增量上报协议（仅当 detector 不可改时）

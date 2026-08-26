# ssgc CLI 鲁棒性问题清单

> 通过在 saitec-guard 环境里实际使用 CLI（init → start → 发请求 → status → report → doctor → stop → restart）逐个暴露并记录。

| # | 级别 | 模块 | 现象 | 根因 | 状态 |
|---|------|------|------|------|------|
| 1 | P0 | cli/_serve.py | `ssgc start` 返回 ok 但子进程秒崩，端口全无 | `_serve.py` 顶层使用 `from ..runtime.runtime import Runtime` 相对导入；被 `start.py` 以 `python _serve.py <path>` 方式 fork 调用时无父包，ImportError | **已修**：改为 `from ssgc.runtime.runtime import Runtime` 绝对导入 |
| 2 | P1 | cli/commands/doctor.py | 服务运行中 `doctor` 报 `port:9001/9002/9003 status:fail` | `_check_port_free()` 用 `bind()` 检测端口空闲，但 service 自身正在监听，bind 必然失败被误判 | **已修**：新增 `_check_port(port, service_running)`，服务运行时用 `connect` 验证被监听；未运行时仍用 `bind` 验证可绑 |
| 3 | P1 | cli/main.py | 中文 help/报错在 GBK 代码页下显示 `?` | Python 在 Windows pipe 模式下 stdout 编码 fallback 到 gbk，与 source 的 UTF-8 中文字符串编码冲突 | **已修**：CLI 入口 `_configure_io_encoding()` —— pipe 时强制 UTF-8（Agent JSON 解析必须），tty 时跟随系统编码 |
| 4 | P2 | cli/commands/status.py | `queue_depth: N/A`（人类可读形态） | 硬编码字符串误导用户，实际 status 命令通过 PID 文件从外部读取，无法获取 queue_depth | **已修**：删除 `queue_depth` 字段（避免误导），等未来有 IPC 时再加 |
| 5 | P2 | cli/commands/doctor.py | `detail` 字段的中文（"已配置"/"需要 ≥1GB"）被编码成 GBK 后在终端乱码 | 同问题 #3 | **同 #3**：编码重配后 doctor 中文 detail 正常显示 |
| 6 | P2 | reporter/reporter.py + runtime/runtime.py | AUTH 失败时报 "X-API-Key 失效，停止上报：请重新 init" 但未告知用户预期值和来源 | reporter 抛 ReportError 后 runtime 直接覆盖了原 message；message 也不包含 URL / 排查建议 | **已修**：(a) reporter 的 AUTH message 加上 `{url}` + init 命令建议；(b) runtime 的 `_report_loop` 和 `_replay_unreported` 都用 `logger.error("X-API-Key 失效，停止上报：%s", e.message)` 透传 |
| 7 | P2 | cli/commands/init.py | api_key 任意字符串直接写入，未做基础格式校验（如非空、最小长度） | 容易拼写错后上线发现 401 | **已修**：init 校验 api_key 长度 ≥8 + detector_url 以 http/https 开头，否则 exit 1 + 中文错误信息 |
| 8 | P3 | cli/commands/init.py | Windows 下 config.json 默认继承父目录权限，其他用户可读 | `_set_file_private` 在 Windows 上直接 `return`（要求管理员），仅给用户文字 warning | **未修**：自动 icacls 需要管理员权限或脚本提权，普通 init 不能做；保留 warning 提示手动 |
| 9 | P0 | proxy/server.py | 客户端（openai SDK）全部报 `Connection error.`（每条 ~6s，SDK 内部重试 2 次）；但代理侧 monitor/JSONL 全 200 且内容完整 | 上游（DeepSeek 等）gzip 压缩响应，aiohttp `ClientSession(auto_decompress=True)` 已解压 body，但 `Content-Encoding: gzip` 头被原样透传——客户端按 gzip 解码明文失败。本地不压缩的上游不触发，换压缩上游才暴露 | **已修**（2026-08-26）：响应头剥离集合加 `content-encoding`（非流式与 SSE 两处，提取 `_STRIP_RESPONSE_HEADERS` 常量）；回归测试 `test_proxy_strips_content_encoding_from_gzip_upstream` 等 2 例 |

## 修复原则

- P0/P1：必须修（影响核心流程/自检判断）
- P2：交付前修（用户能看到的瑕疵）
- P3：可接受留 warning（涉及提权等）

## 验证方法（端到端实操过的链路）

1. 起 mock detector + mock upstream → init → start → curl 发请求 → status / doctor / report 验证
2. 故意填错 api_key 验证 AUTH 错误信息完整
3. 故意填短 api_key / 非法 URL 验证 init 校验
4. `ssgc stop` 优雅关闭
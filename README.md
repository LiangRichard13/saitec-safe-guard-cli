# Saitec Safe Guard CLI

> 监控大模型 API 调用的反向代理 CLI：让"请求/响应"经过本工具，便于安全检测与审计。

把 Claude Code / Codex / 自写脚本的大模型请求指到本地 9001-9003 端口，`ssgc` 会透明转发到上游、按周期上报归一化记录到内部检测服务器、写检测结果到本地 SQLite。

## 快速上手

```bash
pip install saitec-safe-guard-cli

# --upstream 指明要监控的大模型端点（官方 / 其他厂商兼容口 / 本地模型均可）
ssgc init --api-key "<KEY>" --detector-url "http://detector:8080" \
    --upstream "https://api.deepseek.com/anthropic"

# 要监控更多端点？逐个加：
ssgc service add local-llm --upstream http://localhost:23333

ssgc start

# 按输出提示把客户端 base_url 指到本地端口即可（OPENAI_BASE_URL / ANTHROPIC_BASE_URL）
```

支持的 upstream 形态：官方端点（`https://api.openai.com`）、厂商兼容口（`https://api.deepseek.com/anthropic`）、中转网关（`https://opencode.ai/zen/go/v1`）、本地模型（`http://localhost:23333`）——任何 OpenAI / Anthropic 兼容端点。

详细使用见 **[`docs/user-guide.md`](docs/user-guide.md)**——含完整命令参考、配置详解、集成示例（Claude Code / Codex / 自写客户端）、排错手册、安全注意事项。

**检测服务器接口开发人员**请看 **[`docs/integration/detector-api.md`](docs/integration/detector-api.md)**——上报请求/响应契约、状态码与重试语义、幂等性要求、最小实现参考。

## 状态

**v0.1.0 — 初版交付水平**。15 个命令全部可用；端到端链路（init→start→monitor→发请求→JSONL→上报→SQLite→report→stop）在 mock detector 上验证过；250 个 pytest 全绿。

开发约定见 [AGENTS.md](AGENTS.md)（分层原则/测试/文档矩阵/git 规范），项目进度见 [PROGRESS.md](PROGRESS.md)。已修复的鲁棒性问题（端到端联调发现）见 [`docs/issues/cli-usage-issues.md`](docs/issues/cli-usage-issues.md)。

## 命令速查

| 类别 | 命令 |
|---|---|
| 配置 | `init` · `validate` · `config get/set/unset/list` · `service add/remove/set/list` |
| 生命周期 | `start` · `monitor` · `stop` · `restart` · `status` · `logs` |
| 运维 | `report` · `redo` · `purge` · `export` |
| 调试 | `doctor` · `tail` |

每个命令都支持 `--json` 输出（Agent 友好，`monitor` 除外——它是给人看的实时流）和 `--config <path>`（自定义配置文件位置）。`ssgc monitor` 为前台实时监控：正常流量灰色简报、violation/上报失败彩色醒目，适合安全值守场景。

## 安装选项

```bash
# 仅 CLI
pip install saitec-safe-guard-cli

# 含 dev 依赖（测试 + mypy）
pip install "saitec-safe-guard-cli[dev]"

# 含 mock detector（本地联调用）
pip install "saitec-safe-guard-cli[mock]"
```

源码安装：

```bash
git clone https://github.com/LiangRichard13/saitec-safe-guard-cli.git
cd saitec-safe-guard-cli
pip install -e ".[dev]"
```

## 设计文档

实现原理通俗导览见 [`docs/how-it-works.md`](docs/how-it-works.md)（数据流/六层架构/关键机制/设计取舍）。详细设计见 `docs/design/`：

- `saitec-safe-guard-cli-design.md` — 总体设计
- `architecture.md` — 6 层架构与代码组织
- `data-model.md` — SQLite + JSONL 数据模型

## 项目结构

```
src/ssgc/
├── core/         # Layer 1：数据模型 + 配置 + 路径 + 工具
├── recorder/     # Layer 2：记录器（JSONL 落盘）
├── reporter/     # Layer 2：上报器（HTTP POST）
├── store/        # Layer 2：存储（SQLite）
├── adapters/     # Layer 3：协议适配（OpenAI Chat Completions / OpenAI Responses / Anthropic Messages）
├── proxy/        # Layer 4：反向代理核心
├── runtime/      # Layer 5：运行时编排
└── cli/          # Layer 6：CLI 入口

tests/
├── test_adapters/    # 协议适配单元测试
├── test_cli/         # CLI 命令测试
├── test_core/        # 数据模型 + 配置测试
├── test_mock/        # mock 检测服务器自身接口测试
├── test_proxy/       # 反向代理测试
├── test_recorder/    # 记录器测试
├── test_reporter/    # 上报器测试
├── test_runtime/     # 运行时编排测试
├── test_store/       # SQLite 测试
└── mock_detector/     # 本地 mock 检测服务器（FastAPI）
```

## 项目命名

- **GitHub / PyPI**：`saitec-safe-guard-cli`
- **Python 包**：`ssgc`（代码内简写，与 CLI 命令一致）
- **CLI 命令**：`ssgc`

## 许可证

内部项目，保留所有权利。
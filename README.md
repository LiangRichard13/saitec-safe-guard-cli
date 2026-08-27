<div align="center">

<img src="docs/assets/logo.svg" alt="SSGC Logo" width="128"/>

# SSGC · Safe Guard CLI

**大模型 API 流量的安全哨兵** —— 反向代理透明转发，请求/响应全量审计

[![PyPI](https://img.shields.io/badge/pypi-saitec--safe--guard--cli-3775A9?style=flat-square&logo=pypi&logoColor=white)](https://pypi.org/project/saitec-safe-guard-cli/)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)](pyproject.toml)
[![Tests](https://img.shields.io/badge/tests-252%20passed-2EA043?style=flat-square)](tests/)
[![License](https://img.shields.io/badge/license-proprietary-CB3627?style=flat-square)](LICENSE)

[快速上手](#-快速上手) · [特性](#-特性) · [工作原理](#-工作原理) · [命令速查](#-命令速查) · [用户手册](docs/user-guide.md) · [Detector 对接](docs/integration/detector-api.md)

</div>

---

把 Claude Code / Codex / 自写脚本的 LLM 请求指到本地端口（9001 起），`ssgc` 在本机做透明反向代理：请求原样转发到真实上游，同时把每一轮对话**归一化记录**下来，按周期上报到内部安全检测服务器，检测结果落本地 SQLite——随时查询、导出审计报告。

```text
$ ssgc start
 ____   ____    ____    ____
/ ___| / ___|  / ___|  / ___|
\___ \ \___ \ | |   _ | |  _
 ___) |  ___) || |_/ || |_/ |
|____/ |____/  \____|  \____|

🛡️ 服务已启动 · PID 11056
📡 服务映射（客户端 base_url → 本地端口 → 真实上游）
  1. deepseek-openai-compatible  127.0.0.1:9001 → https://api.deepseek.com
     客户端配置: OPENAI_BASE_URL=http://127.0.0.1:9001/v1
检测服务器: http://127.0.0.1:8000 · 上报周期 60s

$ ssgc report          # 查检测结果（clean/violation 一目了然）
$ ssgc export -f html  # 导出审计报告（可打印转 PDF）
```

## ✨ 特性

| | |
|---|---|
| 🔒 **透明代理** | 不改写任何内容、不做 MITM；客户端只需换一个 `base_url` |
| 🧩 **三协议适配** | OpenAI Chat Completions · OpenAI Responses · Anthropic Messages，SSE 流式完整重组 |
| 📝 **全量留痕** | 每轮对话归一化记录 JSONL 按天落盘——先落盘后上报，崩溃不丢数据 |
| 🔄 **断点续传** | 上报游标 + 启动重放：detector 宕机恢复后自动补报积压 |
| 📊 **检测留档** | 结论写本地 SQLite；`export` 一键导出 Markdown/HTML 审计报告 |
| 🛰️ **实时监控** | `monitor` 前台彩色面板给人盯，`report --json` 给 Agent 巡检 |
| 🤖 **Agent 友好** | 全命令 `--json` 结构化输出，内置 [Agent 操作 Skill](#-agent-集成) |

## 🚀 快速上手

```bash
pip install saitec-safe-guard-cli

# 1. 初始化：指明要监控的大模型端点（官方/兼容口/中转/本地均可）
ssgc init --api-key "<KEY>" --detector-url "http://detector:8080" \
    --upstream "https://api.deepseek.com"

# 2. 启动（按输出提示把客户端 base_url 指到本地端口）
ssgc start

# 3. 等一个上报周期后查结果
ssgc report
```

支持任意 OpenAI / Anthropic 兼容上游：

<details>
<summary>📦 更多安装选项</summary>

```bash
pip install "saitec-safe-guard-cli[dev]"     # + 测试/mypy
pip install "saitec-safe-guard-cli[mock]"    # + mock detector 联调桩
pip install "saitec-safe-guard-cli[probe]"   # + 三协议真实联调探针
```

源码开发：

```bash
git clone https://github.com/LiangRichard13/saitec-safe-guard-cli.git
cd saitec-safe-guard-cli && pip install -e ".[dev]"
```

</details>

## 🛤️ 支持的上游形态

| 场景 | `--upstream` 示例 |
|------|-------------------|
| OpenAI / Anthropic 官方 | `https://api.openai.com` |
| 厂商兼容口 | `https://api.deepseek.com/anthropic` |
| 中转网关 | `https://opencode.ai/zen/go/v1` |
| 本地模型 | `http://localhost:23333` |

## ⚙️ 工作原理

```text
你的客户端               ssgc（本机）                           外部
─────────              ────────────────────                    ────
Claude Code ──请求──→ ① 本地代理端口(9001..)
                        │  透明转发（不动内容）
                        │                     ──原请求──→  真实上游
                        │                     ←──响应────  (DeepSeek等)
                        │  ←──响应原样回给客户端
                        ↓
                     ② 边转发边"抄写"：拼出归一化记录(Record)
                        ↓
                     ③ 内存队列 → 周期落盘 JSONL（先落盘，防丢）
                        ↓
                     ④ 每 60s 取一批 POST ──────────────→  检测服务器
                        │                                    （X-API-Key）
                     ⑤ 结论写入本地 SQLite ←──────────────  results[] 
                        ↓
                   ssgc report 查询 · ssgc export 导出审计报告
```

实现细节见 [`docs/how-it-works.md`](docs/how-it-works.md)（通俗版）/ `docs/design/`（正式规格）。

## 📋 命令速查

| 类别 | 命令 |
|---|---|
| 配置 | `init` · `validate` · `config get/set/unset/list` · `service add/remove/set/list` |
| 生命周期 | `start` · `monitor` · `stop` · `restart` · `status` · `logs` |
| 运维 | `report` · `redo` · `purge` · `export` |
| 调试 | `doctor` · `tail` |

每个命令都支持 `--json`（Agent 契约：字段只增不减）与 `--config <path>`（多实例隔离）。完整用法与排错见 **[`docs/user-guide.md`](docs/user-guide.md)**。

## 🤖 Agent 集成

内置 Agent 操作指南（SKILL.md + references + evals）：让 Claude Code 等 AI 工具正确驱动本 CLI 的全部功能。clone 后建本地链接即可启用：

```bash
# Windows（junction，无需管理员）
cmd /c mklink /J "<repo>\.claude\skills\ssgc" "<repo>\skills\ssgc"
# Unix
mkdir -p .claude/skills && ln -s ../../skills/ssgc .claude/skills/ssgc
```

## 🗂️ 项目结构

```text
src/ssgc/
├── core/       # L1 数据模型 + 配置三级覆盖
├── recorder/ reporter/ store/   # L2 JSONL 落盘 · HTTP 上报 · SQLite
├── adapters/   # L3 三协议解析（SSE 流重组）
├── proxy/      # L4 反向代理核心（catch-all 透传）
├── runtime/    # L5 编排：上报循环/游标续传/event_sink
└── cli/        # L6 typer 入口 + 15 个命令

tests/          # 镜像分层单测 + mock_detector 联调桩 + test_chat 三协议探针 + verification 双重验证清单
docs/           # user-guide / how-it-works / design / integration
```

开发约定见 [AGENTS.md](AGENTS.md)，项目进度见 [PROGRESS.md](PROGRESS.md)。

## 项目命名

- **GitHub / PyPI**: `saitec-safe-guard-cli`
- **CLI 命令 / Python 包**: `ssgc`
- **品牌**: SSGC

## 许可证

内部项目，保留所有权利——详见 [LICENSE](LICENSE)。

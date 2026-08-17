# Saitec Safe Guard CLI

> 监控大模型 API 调用的反向代理 CLI：让"请求/响应"经过本工具，便于安全检测与审计。

## 状态

设计阶段 → 骨架阶段。代码骨架已就位，业务逻辑后续按 Phase A-E 落地。

## 设计文档

详细设计见 `docs/design/`：

- `saitec-safe-guard-cli-design.md` — 总体设计
- `architecture.md` — 6 层架构与代码组织
- `data-model.md` — SQLite + JSONL 数据模型

## 安装

```bash
pip install -e ".[dev]"
```

## 使用

```bash
safe-guard init
safe-guard start
safe-guard status
safe-guard report
```

13 个命令（详见 `docs/design/saitec-safe-guard-cli-design.md` §13）：

| 类别 | 命令 |
|---|---|
| 配置 | `init / validate / config` |
| 生命周期 | `start / stop / restart / status / logs` |
| 运维 | `report / redo / purge` |
| 调试 | `doctor / tail` |

## 项目结构

```
src/saitec/
├── core/         # Layer 1：数据模型 + 配置 + 路径 + 工具
├── recorder/     # Layer 2：记录器（JSONL 落盘）
├── reporter/     # Layer 2：上报器（HTTP POST）
├── store/        # Layer 2：存储（SQLite）
├── adapters/      # Layer 3：协议适配（OpenAI/Anthropic）
├── proxy/        # Layer 4：反向代理核心
├── runtime/      # Layer 5：运行时编排
└── cli/          # Layer 6：CLI 入口
```

## 项目命名

- **GitHub / PyPI**：`saitec-safe-guard-cli`
- **Python 包**：`saitec`（命名空间，公司名）
- **CLI 命令**：`safe-guard`

## 许可证

内部项目，保留所有权利。
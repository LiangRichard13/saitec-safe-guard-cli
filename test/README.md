# test/ — 手动测试与 spike 脚本

**与 `tests/` 的区别**：

| 目录 | 用途 | 运行方式 |
|---|---|---|
| `tests/` | pytest 单元测试，每次提交前 `pytest` 跑通 | 自动 |
| `test/` | 手动调试 / spike / 集成验证脚本 | 手动按需 |

## 计划收录的脚本

- `spike_capture_sse.py` — 捕获三种 LLM 端点的真实 SSE 流（Phase C 之前需做）

## 使用约定

- 文件名加 `spike_` 前缀表示一次性探索
- 完成的 spike 把结论写到 `docs/design/`，然后该脚本可删除或保留为参考
- 不向 `test/` 放长期维护的测试脚本（那些在 `tests/`）
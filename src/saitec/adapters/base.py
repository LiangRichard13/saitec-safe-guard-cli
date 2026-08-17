"""Adapter 抽象基类 — Layer 3

协议适配层的统一接口。Adapter 是**纯函数式 + 状态对象**，无 IO。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Adapter(ABC):
    """LLM 协议适配器

    负责：解析请求、累积流式 chunk、重组完整响应与 usage。

    鲁棒性契约（见 `docs/design/architecture.md` §4 Layer 3）：
    - `on_stream_chunk` 绝不抛异常；坏数据 → 内部累计 `error_chunk_count` 跳过
    - `finalize` 必须被调用一次（即使上游中断）；最终 `Record.error` 字段记录流量完整性
    """

    endpoint_type: str

    @abstractmethod
    def parse_request(self, body: bytes) -> dict[str, Any]:
        """解析请求体为结构化 dict（model / messages / tools 等）"""
        ...

    @abstractmethod
    def on_stream_chunk(self, chunk: bytes) -> None:
        """累积一个流式 chunk；**不抛异常**"""
        ...

    @abstractmethod
    def finalize(self) -> dict[str, Any]:
        """返回 `{content, finish_reason, usage, raw}`

        自身异常由 proxy 兜底（content=raw_buffer, usage=None）。
        """
        ...

    @abstractmethod
    def is_terminal(self) -> bool:
        """终止标记检测（区分上游完成 vs 流中断）"""
        ...
"""Adapter 抽象基类 — Layer 3

协议适配层的统一接口。Adapter 是**纯函数式 + 状态对象**，无 IO。
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any, Iterator


class Adapter(ABC):
    """LLM 协议适配器

    负责：解析请求、累积流式 chunk、重组完整响应与 usage。

    鲁棒性契约（见 `docs/design/architecture.md` §4 Layer 3）：
    - `on_stream_chunk` 绝不抛异常；坏数据 → 内部累计 `error_chunk_count` 跳过
    - `finalize` 必须被调用一次（即使上游中断）；最终 `Record.error` 字段记录流量完整性
    """

    endpoint_type: str

    # 跨 chunk 行缓冲：SSE 的 data:/event: 行可能被 TCP 分片切成两半，
    # 每个 chunk 解码后先拼入缓冲区，按 \n 切出完整行处理（见 P0-1）。
    _line_buffer: str = ""

    def _consume_chunk(self, chunk: bytes) -> Iterator[str]:
        """把 chunk 拼入行缓冲，产出**完整行**（不含末尾残留的半个行）"""
        text = chunk.decode("utf-8", errors="replace").replace("\r\n", "\n")
        self._line_buffer += text
        lines = self._line_buffer.split("\n")
        self._line_buffer = lines.pop()  # 最后一段可能是不完整的行，留到下一个 chunk
        return iter(lines)

    def _try_bare_json(self, chunk: bytes) -> dict[str, Any] | None:
        """非流式响应快速路径：整段正文就是一个 JSON 对象（无 data: 前缀）

        返回解析后的 dict；不是裸 JSON（SSE 流 / 无效内容）返回 None。

        防误判：SSE payload（`data: {...}`）若被 TCP 切分，`data: ` 前缀可能
        在**上一个 chunk**——此时行缓冲有残留（`_line_buffer` 非空），
        说明是流式中段，禁用快速路径走行缓冲。
        """
        if self._line_buffer:
            return None  # 流式中段（有未完成的半个行）→ 不是裸 JSON
        text = chunk.decode("utf-8", errors="replace").strip()
        if not text.startswith("{"):
            return None
        if "data:" in text or "event:" in text or "\n" in text:
            return None
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            return None
        return obj if isinstance(obj, dict) else None

    def _drain_buffer(self) -> Iterator[str]:
        """finalize 前调用：处理缓冲中残留的最后一个不完整行（EOF 时）"""
        if self._line_buffer:
            line, self._line_buffer = self._line_buffer, ""
            return iter([line])
        return iter(())

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
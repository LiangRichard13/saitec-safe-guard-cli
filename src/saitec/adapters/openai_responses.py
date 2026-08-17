"""OpenAI Responses 适配器 — `/v1/responses`

流式 chunk 结构：`data: {...}`，靠 `type` 字段区分事件。
usage 位置：`response.completed` 事件。
"""
from __future__ import annotations

from typing import Any

from .base import Adapter


class OpenAIResponsesAdapter(Adapter):
    endpoint_type = "openai-responses"

    def parse_request(self, body: bytes) -> dict[str, Any]:
        raise NotImplementedError("Phase C 实现")

    def on_stream_chunk(self, chunk: bytes) -> None:
        raise NotImplementedError("Phase C 实现")

    def finalize(self) -> dict[str, Any]:
        raise NotImplementedError("Phase C 实现")

    def is_terminal(self) -> bool:
        raise NotImplementedError("Phase C 实现")
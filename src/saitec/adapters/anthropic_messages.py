"""Anthropic Messages 适配器 — `/v1/messages`

流式 chunk 结构：`event: <type>` + `data: {...}`，`content_block_delta.delta.text`。
usage 位置：`message_delta` 事件。
"""
from __future__ import annotations

from typing import Any

from .base import Adapter


class AnthropicMessagesAdapter(Adapter):
    endpoint_type = "anthropic-messages"

    def parse_request(self, body: bytes) -> dict[str, Any]:
        raise NotImplementedError("Phase C 实现")

    def on_stream_chunk(self, chunk: bytes) -> None:
        raise NotImplementedError("Phase C 实现")

    def finalize(self) -> dict[str, Any]:
        raise NotImplementedError("Phase C 实现")

    def is_terminal(self) -> bool:
        raise NotImplementedError("Phase C 实现")
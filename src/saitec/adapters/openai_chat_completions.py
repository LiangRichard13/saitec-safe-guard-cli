"""OpenAI Chat Completions 适配器 — `/v1/chat/completions`

流式 chunk 结构：`data: {...}`，`choices[0].delta.{role,content}`，结尾 `data: [DONE]`。
usage 位置：末个 chunk（需 `stream_options.include_usage`）或非流式响应体。
"""
from __future__ import annotations

from typing import Any

from .base import Adapter


class OpenAIChatCompletionsAdapter(Adapter):
    endpoint_type = "openai-chat-completions"

    def parse_request(self, body: bytes) -> dict[str, Any]:
        raise NotImplementedError("Phase C 实现")

    def on_stream_chunk(self, chunk: bytes) -> None:
        raise NotImplementedError("Phase C 实现")

    def finalize(self) -> dict[str, Any]:
        raise NotImplementedError("Phase C 实现")

    def is_terminal(self) -> bool:
        raise NotImplementedError("Phase C 实现")
"""Anthropic Messages 适配器 — `/v1/messages`

流式 chunk 结构：`event: <type>` + `data: {...}`。
usage 位置：`message_start.usage.input_tokens` + `message_delta.usage.output_tokens`。
终止：`message_stop` 事件。

鲁棒性契约：on_stream_chunk 永不抛异常。
"""
from __future__ import annotations

import json
from typing import Any

from .base import Adapter


class AnthropicMessagesAdapter(Adapter):
    endpoint_type = "anthropic-messages"

    def __init__(self) -> None:
        self._content: str = ""
        self._finish_reason: str | None = None
        self._usage: dict[str, Any] = {
            "prompt_tokens": None,
            "completion_tokens": None,
        }
        self._error_chunk_count: int = 0
        self._terminal: bool = False
        self._current_event: str | None = None

    def parse_request(self, body: bytes) -> dict[str, Any]:
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return {
            "model": data.get("model"),
            "messages": data.get("messages", []),
            "system": data.get("system"),
            "max_tokens": data.get("max_tokens"),
            "stream": data.get("stream", False),
        }

    def on_stream_chunk(self, chunk: bytes) -> None:
        try:
            text = chunk.decode("utf-8", errors="replace")
        except Exception:
            self._error_chunk_count += 1
            return

        for raw_line in text.splitlines():
            line = raw_line.rstrip("\r")
            if not line:
                continue
            if line.startswith("event:"):
                self._current_event = line[len("event:"):].strip()
                continue
            if line.startswith("data:"):
                payload = line[len("data:"):].strip()
                if not payload:
                    continue
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    self._error_chunk_count += 1
                    continue
                # 优先用 event:，缺失则用 type 字段
                event = self._current_event or obj.get("type", "")
                self._accumulate(event, obj)
                # 处理完一个 data 行后清空 current_event（避免下一个非 event 行继承旧 event）
                self._current_event = None

    def _accumulate(self, event: str, obj: dict[str, Any]) -> None:
        if event == "content_block_delta":
            delta = obj.get("delta") or {}
            if delta.get("type") == "text_delta":
                self._content += delta.get("text", "")
        elif event == "message_delta":
            delta = obj.get("delta") or {}
            fr = delta.get("stop_reason")
            if fr:
                self._finish_reason = fr
            usage = obj.get("usage") or {}
            if usage.get("output_tokens") is not None:
                self._usage["completion_tokens"] = usage["output_tokens"]
        elif event == "message_start":
            message = obj.get("message") or {}
            usage = message.get("usage") or {}
            if usage.get("input_tokens") is not None:
                self._usage["prompt_tokens"] = usage["input_tokens"]
        elif event == "message_stop":
            self._terminal = True

    def finalize(self) -> dict[str, Any]:
        # 只有两个 token 都非 None 才返回 usage，否则 None
        usage = self._usage if any(v is not None for v in self._usage.values()) else None
        return {
            "content": self._content,
            "finish_reason": self._finish_reason,
            "usage": usage,
            "raw": None,
        }

    def is_terminal(self) -> bool:
        return self._terminal
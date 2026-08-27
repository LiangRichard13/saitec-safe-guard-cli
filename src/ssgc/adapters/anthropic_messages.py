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
        self.reset()

    def reset(self) -> None:
        self._content: str = ""
        self._finish_reason: str | None = None
        self._usage: dict[str, Any] = {
            "prompt_tokens": None,
            "completion_tokens": None,
        }
        self._error_chunk_count: int = 0
        self._terminal: bool = False
        self._current_event: str | None = None
        self._line_buffer: str = ""

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
        """累积一个 SSE chunk（跨 chunk 行缓冲）"""
        try:
            bare = self._try_bare_json(chunk)
            if bare is not None:
                self._accumulate_non_stream(bare)
                return
            for line in self._consume_chunk(chunk):
                self._process_line(line)
        except Exception:
            self._error_chunk_count += 1

    def _accumulate_non_stream(self, obj: dict[str, Any]) -> None:
        """非流式响应（content[].text）"""
        for block in obj.get("content", []) or []:
            if block.get("type") == "text":
                text = block.get("text")
                if text:
                    self._content += text
        fr = obj.get("stop_reason")
        if fr:
            self._finish_reason = fr
        usage = obj.get("usage") or {}
        if usage.get("input_tokens") is not None:
            self._usage["prompt_tokens"] = usage["input_tokens"]
        if usage.get("output_tokens") is not None:
            self._usage["completion_tokens"] = usage["output_tokens"]
        self._terminal = True  # 非流式响应天然终止

    def _process_line(self, raw_line: str) -> None:
        line = raw_line.rstrip("\r")
        if not line:
            return
        if line.startswith("event:"):
            self._current_event = line[len("event:"):].strip()
            return
        if line.startswith("data:"):
            payload = line[len("data:"):].strip()
            if not payload:
                return
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                self._error_chunk_count += 1
                return
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
        for line in self._drain_buffer():
            self._process_line(line)
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
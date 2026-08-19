"""OpenAI Chat Completions 适配器 — `/v1/chat/completions`

流式 chunk 结构：`data: {...}`，`choices[0].delta.{role,content}`，结尾 `data: [DONE]`。
usage 位置：末个 chunk（需 `stream_options.include_usage=true`）或非流式响应体。

鲁棒性契约（详见 `architecture.md` §4 Layer 3）：
- `on_stream_chunk` 永不抛异常；坏数据 → 累计 `error_chunk_count` 跳过
"""
from __future__ import annotations

import json
from typing import Any

from .base import Adapter


class OpenAIChatCompletionsAdapter(Adapter):
    endpoint_type = "openai-chat-completions"

    def __init__(self) -> None:
        self._content: str = ""
        self._finish_reason: str | None = None
        self._usage: dict[str, Any] | None = None
        self._error_chunk_count: int = 0
        self._terminal: bool = False

    def parse_request(self, body: bytes) -> dict[str, Any]:
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return {
            "model": data.get("model"),
            "messages": data.get("messages", []),
            "tools": data.get("tools"),
            "stream": data.get("stream", False),
        }

    def on_stream_chunk(self, chunk: bytes) -> None:
        """累积一个 SSE chunk

        chunk 可能含多个 data: 行（甚至跨 event 边界），逐行处理：
        - `data: {...}` → 累积 delta / usage / finish_reason
        - `data: [DONE]` → 标记终止
        - 其他行（event: / 空行 / 注释）→ 跳过
        """
        try:
            text = chunk.decode("utf-8", errors="replace")
        except Exception:
            self._error_chunk_count += 1
            return

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                self._terminal = True
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                self._error_chunk_count += 1
                continue
            self._accumulate(obj)

    def _accumulate(self, obj: dict[str, Any]) -> None:
        # choices 累积 content + finish_reason
        for choice in obj.get("choices", []) or []:
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if content:
                self._content += content
            fr = choice.get("finish_reason")
            if fr:
                self._finish_reason = fr
                # finish_reason 是真正的终止信号（[DONE] 可能不再发）
                self._terminal = True
        # usage（顶层，需 stream_options.include_usage）
        usage = obj.get("usage")
        if usage:
            self._usage = {
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
            }

    def finalize(self) -> dict[str, Any]:
        """返回 `{content, finish_reason, usage, raw}`"""
        return {
            "content": self._content,
            "finish_reason": self._finish_reason,
            "usage": self._usage,
            "raw": None,
        }

    def is_terminal(self) -> bool:
        return self._terminal
"""OpenAI Responses 适配器 — `/v1/responses`

流式 chunk 结构：`data: {...}`，靠 `type` 字段区分事件。
usage 位置：`response.completed` 事件。

鲁棒性契约：on_stream_chunk 永不抛异常。
"""
from __future__ import annotations

import json
from typing import Any

from .base import Adapter


class OpenAIResponsesAdapter(Adapter):
    endpoint_type = "openai-responses"

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
            "input": data.get("input"),
            "instructions": data.get("instructions"),
            "stream": data.get("stream", False),
        }

    def on_stream_chunk(self, chunk: bytes) -> None:
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
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                self._error_chunk_count += 1
                continue
            self._accumulate(obj)

    def _accumulate(self, obj: dict[str, Any]) -> None:
        event_type = obj.get("type", "")
        if event_type == "response.output_text.delta":
            delta = obj.get("delta")
            if isinstance(delta, str):
                self._content += delta
        elif event_type == "response.completed":
            response = obj.get("response") or {}
            usage = response.get("usage") or {}
            if usage:
                self._usage = {
                    "prompt_tokens": usage.get("input_tokens"),
                    "completion_tokens": usage.get("output_tokens"),
                    "total_tokens": usage.get("total_tokens"),
                }
            # finish_reason 在 response.status 字段
            status = response.get("status")
            if status:
                self._finish_reason = status  # completed / failed / incomplete
            self._terminal = True
        elif event_type in ("response.failed", "response.incomplete"):
            response = obj.get("response") or {}
            status = response.get("status")
            if status:
                self._finish_reason = status
            # 也尝试提取 usage
            usage = response.get("usage") or {}
            if usage and not self._usage:
                self._usage = {
                    "prompt_tokens": usage.get("input_tokens"),
                    "completion_tokens": usage.get("output_tokens"),
                    "total_tokens": usage.get("total_tokens"),
                }
            self._terminal = True

    def finalize(self) -> dict[str, Any]:
        return {
            "content": self._content,
            "finish_reason": self._finish_reason,
            "usage": self._usage,
            "raw": None,
        }

    def is_terminal(self) -> bool:
        return self._terminal
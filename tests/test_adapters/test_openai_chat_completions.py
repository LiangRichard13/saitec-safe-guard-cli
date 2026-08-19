"""OpenAI Chat Completions adapter 测试"""
from __future__ import annotations

import json

import pytest

from saitec.adapters.openai_chat_completions import OpenAIChatCompletionsAdapter


# ============================================================
# parse_request
# ============================================================


def test_parse_request_ok() -> None:
    a = OpenAIChatCompletionsAdapter()
    body = json.dumps(
        {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }
    ).encode()
    parsed = a.parse_request(body)
    assert parsed["model"] == "gpt-4o"
    assert parsed["messages"] == [{"role": "user", "content": "hi"}]
    assert parsed["stream"] is True


def test_parse_request_invalid_json_returns_empty() -> None:
    a = OpenAIChatCompletionsAdapter()
    parsed = a.parse_request(b"not json")
    assert parsed == {}


def test_parse_request_preserves_tools() -> None:
    a = OpenAIChatCompletionsAdapter()
    body = json.dumps(
        {
            "model": "gpt-4o",
            "messages": [],
            "tools": [{"type": "function", "function": {"name": "f"}}],
        }
    ).encode()
    parsed = a.parse_request(body)
    assert parsed["tools"] == [{"type": "function", "function": {"name": "f"}}]


# ============================================================
# on_stream_chunk
# ============================================================


def test_accumulate_content() -> None:
    a = OpenAIChatCompletionsAdapter()
    chunk = b'data: {"choices":[{"index":0,"delta":{"content":"Hello"}}]}\n\n'
    a.on_stream_chunk(chunk)
    chunk2 = b'data: {"choices":[{"index":0,"delta":{"content":" world"}}]}\n\n'
    a.on_stream_chunk(chunk2)
    result = a.finalize()
    assert result["content"] == "Hello world"


def test_accumulate_finish_reason() -> None:
    a = OpenAIChatCompletionsAdapter()
    a.on_stream_chunk(
        b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    )
    assert a.finalize()["finish_reason"] == "stop"


def test_accumulate_usage() -> None:
    a = OpenAIChatCompletionsAdapter()
    a.on_stream_chunk(
        b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":7,"total_tokens":12}}\n\n'
    )
    result = a.finalize()
    assert result["usage"] == {
        "prompt_tokens": 5,
        "completion_tokens": 7,
        "total_tokens": 12,
    }


def test_terminal_on_done_marker() -> None:
    a = OpenAIChatCompletionsAdapter()
    a.on_stream_chunk(b'data: [DONE]\n\n')
    assert a.is_terminal()


def test_terminal_finish_reason_stop() -> None:
    """stop 也算终止（实际可能不再发 [DONE]）"""
    a = OpenAIChatCompletionsAdapter()
    a.on_stream_chunk(
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
    )
    assert a.is_terminal()


# ============================================================
# 鲁棒性
# ============================================================


def test_invalid_json_in_chunk_does_not_raise() -> None:
    a = OpenAIChatCompletionsAdapter()
    a.on_stream_chunk(b"data: not json {\n\n")  # 坏 JSON
    # 正常 chunk 仍能累积
    a.on_stream_chunk(
        b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
    )
    result = a.finalize()
    assert result["content"] == "ok"


def test_finalize_with_no_chunks_returns_empty() -> None:
    a = OpenAIChatCompletionsAdapter()
    result = a.finalize()
    assert result["content"] == ""
    assert result["finish_reason"] is None
    assert result["usage"] is None


def test_mixed_valid_and_invalid_lines() -> None:
    a = OpenAIChatCompletionsAdapter()
    sse = (
        b"event: ping\n"  # 未知事件类型
        b": comment\n"  # 注释
        b"\n"  # 空行
        b'data: {"choices":[{"delta":{"content":"a"}}]}\n'
        b"data: bad json\n"  # 坏 JSON
        b'data: {"choices":[{"delta":{"content":"b"}}]}\n'
        b"data: [DONE]\n"
    )
    a.on_stream_chunk(sse)
    result = a.finalize()
    assert result["content"] == "ab"
    assert a.is_terminal()
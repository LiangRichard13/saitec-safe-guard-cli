"""OpenAI Chat Completions adapter 测试"""
from __future__ import annotations

import json

import pytest

from ssgc.adapters.openai_chat_completions import OpenAIChatCompletionsAdapter


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


# ============================================================
# 跨 chunk 行缓冲（P0-1：TCP 分片把 data: 行切成两半）
# ============================================================


def test_half_line_split_across_chunks() -> None:
    """data: 行被 TCP 分片切成两半，应跨 chunk 拼接"""
    a = OpenAIChatCompletionsAdapter()
    first = b'data: {"choices":[{"delta":{"con'
    second = b'tent":"Hello"}}]}\n\n'
    a.on_stream_chunk(first)
    a.on_stream_chunk(second)
    result = a.finalize()
    assert result["content"] == "Hello"


def test_split_right_at_data_prefix() -> None:
    """分片恰好切在 'data:' 前缀中间"""
    a = OpenAIChatCompletionsAdapter()
    a.on_stream_chunk(b'dat')
    a.on_stream_chunk(b'a: {"choices":[{"delta":{"content":"x"}}]}\n\n')
    assert a.finalize()["content"] == "x"


def test_done_marker_split() -> None:
    """[DONE] 标记被切成两半"""
    a = OpenAIChatCompletionsAdapter()
    a.on_stream_chunk(b'data: [DO')
    a.on_stream_chunk(b'NE]\n\n')
    assert a.is_terminal()


def test_multiple_lines_in_one_chunk_plus_buffer() -> None:
    """一个 chunk 含多个完整行 + 末尾半个行，下个 chunk 补齐"""
    a = OpenAIChatCompletionsAdapter()
    a.on_stream_chunk(
        b'data: {"choices":[{"delta":{"content":"one"}}]}\n'
        b'data: {"choices":[{"delta":{"con'
    )
    a.on_stream_chunk(
        b'tent":"two"}}]}\n'
        b'data: [DONE]\n\n'
    )
    result = a.finalize()
    assert result["content"] == "onetwo"
    assert a.is_terminal()


def test_eof_without_trailing_newline() -> None:
    """最后一个 data: 行没有结尾换行（EOF）也应被 finalize 处理"""
    a = OpenAIChatCompletionsAdapter()
    a.on_stream_chunk(
        b'data: {"choices":[{"delta":{"content":"eof"}}]}'
    )  # 无 \n
    result = a.finalize()
    assert result["content"] == "eof"


# ============================================================
# 非流式响应（P0-2：裸 JSON 整段喂给 adapter）
# ============================================================


def test_non_stream_response_parsed() -> None:
    """非流式响应（stream:false）的整段 JSON 应被解析（P0-2 修复）"""
    a = OpenAIChatCompletionsAdapter()
    body = json.dumps(
        {
            "id": "chatcmpl-123",
            "object": "chat.completion",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "Hi there"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        }
    ).encode()
    a.on_stream_chunk(body)
    result = a.finalize()
    assert result["content"] == "Hi there"
    assert result["finish_reason"] == "stop"
    assert result["usage"]["prompt_tokens"] == 5


def test_non_stream_response_is_terminal() -> None:
    """非流式响应天然终止"""
    a = OpenAIChatCompletionsAdapter()
    a.on_stream_chunk(
        json.dumps({"choices": [{"message": {"content": "x"}}]}).encode()
    )
    assert a.is_terminal()
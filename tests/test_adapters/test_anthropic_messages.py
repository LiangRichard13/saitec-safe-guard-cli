"""Anthropic Messages adapter 测试"""
from __future__ import annotations

import json

import pytest

from ssgc.adapters.anthropic_messages import AnthropicMessagesAdapter


def test_parse_request_ok() -> None:
    a = AnthropicMessagesAdapter()
    body = json.dumps(
        {
            "model": "claude-3-5-sonnet",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1024,
            "stream": True,
        }
    ).encode()
    parsed = a.parse_request(body)
    assert parsed["model"] == "claude-3-5-sonnet"
    assert parsed["max_tokens"] == 1024
    assert parsed["stream"] is True


def test_parse_request_invalid_json() -> None:
    a = AnthropicMessagesAdapter()
    assert a.parse_request(b"not json") == {}


def test_parse_request_preserves_system() -> None:
    """Anthropic API 的 system 字段独立于 messages"""
    a = AnthropicMessagesAdapter()
    body = json.dumps(
        {
            "model": "claude-3-5-sonnet",
            "system": "You are a helpful assistant.",
            "messages": [],
            "max_tokens": 1024,
        }
    ).encode()
    parsed = a.parse_request(body)
    assert parsed["system"] == "You are a helpful assistant."


def test_accumulate_text_delta() -> None:
    a = AnthropicMessagesAdapter()
    a.on_stream_chunk(
        b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}\n\n'
    )
    a.on_stream_chunk(
        b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":" world"}}\n\n'
    )
    assert a.finalize()["content"] == "Hello world"


def test_usage_split_across_events() -> None:
    """Anthropic 的 input_tokens 在 message_start，output_tokens 在 message_delta"""
    a = AnthropicMessagesAdapter()
    a.on_stream_chunk(
        b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":12}}}\n\n'
    )
    a.on_stream_chunk(
        b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":34}}\n\n'
    )
    result = a.finalize()
    assert result["usage"]["prompt_tokens"] == 12
    assert result["usage"]["completion_tokens"] == 34
    assert result["finish_reason"] == "end_turn"


def test_terminal_on_message_stop() -> None:
    a = AnthropicMessagesAdapter()
    a.on_stream_chunk(b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
    assert a.is_terminal()


def test_full_anthropic_sequence() -> None:
    """模拟一次完整的 Anthropic 流"""
    a = AnthropicMessagesAdapter()
    chunks = [
        b'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1","usage":{"input_tokens":8,"output_tokens":1}}}\n\n',
        b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n',
        b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hi"}}\n\n',
        b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":" there"}}\n\n',
        b'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n',
        b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":4}}\n\n',
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
    ]
    for c in chunks:
        a.on_stream_chunk(c)
    result = a.finalize()
    assert result["content"] == "Hi there"
    assert result["finish_reason"] == "end_turn"
    assert result["usage"]["prompt_tokens"] == 8
    assert result["usage"]["completion_tokens"] == 4
    assert a.is_terminal()


def test_invalid_json_does_not_raise() -> None:
    a = AnthropicMessagesAdapter()
    a.on_stream_chunk(b'event: content_block_delta\ndata: bad\n\n')
    a.on_stream_chunk(
        b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"ok"}}\n\n'
    )
    assert a.finalize()["content"] == "ok"


def test_event_line_state_carries_to_next_data() -> None:
    """event: 行单独一个 chunk 时，current_event 应保持到下一个 data: 行"""
    a = AnthropicMessagesAdapter()
    a.on_stream_chunk(b"event: content_block_delta\n")  # 单独一行
    a.on_stream_chunk(
        b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"across"}}\n\n'
    )
    assert a.finalize()["content"] == "across"


def test_event_state_resets_between_events() -> None:
    """event: 处理后 current_event 清空，避免污染下个 data"""
    a = AnthropicMessagesAdapter()
    # event 1: content_block_delta
    a.on_stream_chunk(
        b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"a"}}\n\n'
    )
    # event 2: message_stop（不应继承上一个 event 的状态）
    a.on_stream_chunk(b'event: ping\ndata: {"type":"ping"}\n\n')
    # event 3: message_delta（应能正常累积 content_delta，即使中间混了 message_stop）
    # 注意 message_delta 不累积 content
    a.on_stream_chunk(
        b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
    )
    result = a.finalize()
    assert result["content"] == "a"  # 只第一个 event 累积了
    assert result["finish_reason"] == "end_turn"


def test_finalize_no_chunks() -> None:
    a = AnthropicMessagesAdapter()
    result = a.finalize()
    assert result["content"] == ""
    assert result["finish_reason"] is None
    assert result["usage"] is None


def test_partial_usage_finalize_returns_none() -> None:
    """只收到 input_tokens 没有 output_tokens 时，usage 仍返回（包含 None 字段）"""
    a = AnthropicMessagesAdapter()
    a.on_stream_chunk(
        b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":5}}}\n\n'
    )
    result = a.finalize()
    # 即使只有一个 token，也返回 usage（保持信息）
    assert result["usage"] is not None
    assert result["usage"]["prompt_tokens"] == 5
    assert result["usage"]["completion_tokens"] is None


# ============================================================
# 跨 chunk 行缓冲（P0-1）
# ============================================================


def test_event_line_split_across_chunks() -> None:
    """event: 行被 TCP 分片切成两半"""
    a = AnthropicMessagesAdapter()
    a.on_stream_chunk(b"event: content_block_del")
    a.on_stream_chunk(
        b'ta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"across"}}\n\n'
    )
    assert a.finalize()["content"] == "across"


def test_data_line_split_across_chunks() -> None:
    """data: 行的 JSON 被切成两半"""
    a = AnthropicMessagesAdapter()
    a.on_stream_chunk(
        b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","'
    )
    a.on_stream_chunk(b'text":"split"}}\n\n')
    assert a.finalize()["content"] == "split"


def test_event_and_data_both_split() -> None:
    """event: 行和紧随的 data: 行都被拆开"""
    a = AnthropicMessagesAdapter()
    a.on_stream_chunk(b"event: message_")
    a.on_stream_chunk(
        b'start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":9}'
    )
    a.on_stream_chunk(b"}}\n\n")
    result = a.finalize()
    assert result["usage"]["prompt_tokens"] == 9


def test_non_stream_response_parsed() -> None:
    """非流式响应（content[].text）应被解析（P0-2 修复）"""
    a = AnthropicMessagesAdapter()
    body = json.dumps(
        {
            "id": "msg_1",
            "type": "message",
            "content": [
                {"type": "text", "text": "Hello anthropic"},
            ],
            "usage": {"input_tokens": 8, "output_tokens": 4},
            "stop_reason": "end_turn",
        }
    ).encode()
    a.on_stream_chunk(body)
    result = a.finalize()
    assert result["content"] == "Hello anthropic"
    assert result["finish_reason"] == "end_turn"
    assert result["usage"]["prompt_tokens"] == 8
    assert result["usage"]["completion_tokens"] == 4
    assert a.is_terminal()


def test_anthropic_sse_payload_not_misdetected_as_bare_json() -> None:
    """SSE 的 data: payload 单独成 chunk 时不应走裸 JSON 快速路径"""
    a = AnthropicMessagesAdapter()
    # 模拟 TCP 把 `data: ` 与 payload 分开（payload 以 { 开头）
    a.on_stream_chunk(b"data: ")
    a.on_stream_chunk(
        b'{"type":"content_block_delta","delta":{"type":"text_delta","text":"ok"}}\n\n'
    )
    result = a.finalize()
    assert result["content"] == "ok"
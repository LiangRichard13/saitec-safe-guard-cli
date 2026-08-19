"""OpenAI Responses adapter 测试"""
from __future__ import annotations

import json

import pytest

from saitec.adapters.openai_responses import OpenAIResponsesAdapter


def test_parse_request_ok() -> None:
    a = OpenAIResponsesAdapter()
    body = json.dumps(
        {"model": "gpt-4o", "input": "hello", "stream": True}
    ).encode()
    parsed = a.parse_request(body)
    assert parsed["model"] == "gpt-4o"
    assert parsed["input"] == "hello"
    assert parsed["stream"] is True


def test_parse_request_invalid_json() -> None:
    a = OpenAIResponsesAdapter()
    assert a.parse_request(b"garbage") == {}


def test_accumulate_text_delta() -> None:
    a = OpenAIResponsesAdapter()
    a.on_stream_chunk(
        b'data: {"type":"response.output_text.delta","delta":"Hello"}\n\n'
    )
    a.on_stream_chunk(
        b'data: {"type":"response.output_text.delta","delta":" world"}\n\n'
    )
    assert a.finalize()["content"] == "Hello world"


def test_terminal_on_completed() -> None:
    a = OpenAIResponsesAdapter()
    a.on_stream_chunk(
        b'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":5,"output_tokens":7,"total_tokens":12}}}\n\n'
    )
    assert a.is_terminal()
    result = a.finalize()
    assert result["finish_reason"] == "completed"
    assert result["usage"] == {
        "prompt_tokens": 5,
        "completion_tokens": 7,
        "total_tokens": 12,
    }


def test_terminal_on_failed() -> None:
    a = OpenAIResponsesAdapter()
    a.on_stream_chunk(
        b'data: {"type":"response.failed","response":{"status":"failed"}}\n\n'
    )
    assert a.is_terminal()
    assert a.finalize()["finish_reason"] == "failed"


def test_terminal_on_incomplete() -> None:
    a = OpenAIResponsesAdapter()
    a.on_stream_chunk(
        b'data: {"type":"response.incomplete","response":{"status":"incomplete"}}\n\n'
    )
    assert a.is_terminal()
    assert a.finalize()["finish_reason"] == "incomplete"


def test_full_event_sequence() -> None:
    """模拟一次完整的 Responses API 流"""
    a = OpenAIResponsesAdapter()
    events = [
        b'data: {"type":"response.created","response":{"id":"resp_1"}}\n\n',
        b'data: {"type":"response.output_item.added","item":{"type":"message"}}\n\n',
        b'data: {"type":"response.content_part.added","part":{"type":"output_text"}}\n\n',
        b'data: {"type":"response.output_text.delta","delta":"Hi"}\n\n',
        b'data: {"type":"response.output_text.delta","delta":" there"}\n\n',
        b'data: {"type":"response.content_part.done"}\n\n',
        b'data: {"type":"response.output_item.done"}\n\n',
        b'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":10,"output_tokens":3}}}\n\n',
    ]
    for e in events:
        a.on_stream_chunk(e)
    result = a.finalize()
    assert result["content"] == "Hi there"
    assert result["usage"]["prompt_tokens"] == 10
    assert result["usage"]["completion_tokens"] == 3


def test_invalid_json_does_not_raise() -> None:
    a = OpenAIResponsesAdapter()
    a.on_stream_chunk(b"data: {bad json\n\n")
    a.on_stream_chunk(b'data: {"type":"response.output_text.delta","delta":"x"}\n\n')
    assert a.finalize()["content"] == "x"


def test_unknown_event_type_ignored() -> None:
    a = OpenAIResponsesAdapter()
    a.on_stream_chunk(b'data: {"type":"response.unknown_event","foo":"bar"}\n\n')
    result = a.finalize()
    assert result["content"] == ""
    assert not a.is_terminal()


def test_finalize_no_chunks() -> None:
    a = OpenAIResponsesAdapter()
    result = a.finalize()
    assert result["content"] == ""
    assert result["finish_reason"] is None
    assert result["usage"] is None


def test_half_line_split_across_chunks() -> None:
    """data: 行的 JSON 被 TCP 分片切成两半（P0-1）"""
    a = OpenAIResponsesAdapter()
    a.on_stream_chunk(
        b'data: {"type":"response.output_text.delta","del'
    )
    a.on_stream_chunk(b'ta":"Hello"}\n\n')
    assert a.finalize()["content"] == "Hello"


def test_non_stream_response_parsed() -> None:
    """非流式响应（output[].content[].text）应被解析（P0-2 修复）"""
    a = OpenAIResponsesAdapter()
    body = json.dumps(
        {
            "id": "resp_1",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": "Hello response"},
                    ],
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 3},
        }
    ).encode()
    a.on_stream_chunk(body)
    result = a.finalize()
    assert result["content"] == "Hello response"
    assert result["finish_reason"] == "completed"
    assert result["usage"]["prompt_tokens"] == 10
    assert a.is_terminal()
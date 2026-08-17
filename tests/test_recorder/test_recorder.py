"""Recorder（内存队列 + JSONL 落盘）测试"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from saitec.core.models import Record
from saitec.recorder.recorder import Recorder


def _make_record(idx: int) -> Record:
    return Record(
        record_id=f"rec-{idx:04d}",
        service="svc",
        endpoint_type="openai-chat-completions",
        upstream="https://api.openai.com",
        path="/v1/chat/completions",
        timestamp="2026-08-14T12:00:00Z",
        elapsed_ms=100,
        status_code=200,
        error=None,
        request={"model": "gpt-4o", "messages": []},
        response={"content": "hi", "usage": {}, "finish_reason": "stop"},
    )


# ============================================================
# 同步 API
# ============================================================


async def test_enqueue_increments_depth(tmp_path: Path) -> None:
    r = Recorder(tmp_path, batch_size=5)
    r.enqueue(_make_record(1))
    r.enqueue(_make_record(2))
    assert r.queue_depth() == 2


async def test_flush_drains_batch(tmp_path: Path) -> None:
    r = Recorder(tmp_path, batch_size=3)
    for i in range(1, 8):
        r.enqueue(_make_record(i))
    batch = await r.flush()
    assert len(batch) == 3
    assert [b.record_id for b in batch] == ["rec-0001", "rec-0002", "rec-0003"]
    assert r.queue_depth() == 4


async def test_flush_returns_remaining_when_below_batch_size(tmp_path: Path) -> None:
    r = Recorder(tmp_path, batch_size=100)
    for i in range(1, 4):
        r.enqueue(_make_record(i))
    batch = await r.flush()
    assert len(batch) == 3


async def test_flush_empty_returns_empty(tmp_path: Path) -> None:
    r = Recorder(tmp_path)
    assert await r.flush() == []


# ============================================================
# 队列上限
# ============================================================


async def test_overflow_drops_oldest(tmp_path: Path) -> None:
    r = Recorder(tmp_path, batch_size=10, max_queue_size=5)
    for i in range(1, 8):
        r.enqueue(_make_record(i))
    assert r.queue_depth() == 5
    assert r.dropped_count() == 2
    batch = await r.flush()
    assert [b.record_id for b in batch] == [
        "rec-0003",
        "rec-0004",
        "rec-0005",
        "rec-0006",
        "rec-0007",
    ]


async def test_overflow_in_bursts(tmp_path: Path) -> None:
    r = Recorder(tmp_path, batch_size=10, max_queue_size=3)
    for i in range(1, 6):
        r.enqueue(_make_record(i))
    assert r.queue_depth() == 3
    assert r.dropped_count() == 2


# ============================================================
# 落盘（flush() 同时负责落盘）
# ============================================================


async def test_flush_writes_jsonl(tmp_path: Path) -> None:
    r = Recorder(tmp_path, batch_size=10)
    for i in range(1, 4):
        r.enqueue(_make_record(i))
    await r.flush()

    files = list(tmp_path.glob("records-*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text().strip().splitlines()
    assert len(lines) == 3
    parsed = [json.loads(line) for line in lines]
    assert [p["record_id"] for p in parsed] == ["rec-0001", "rec-0002", "rec-0003"]


async def test_flush_no_records_no_jsonl(tmp_path: Path) -> None:
    r = Recorder(tmp_path)
    await r.flush()  # 空 flush 不写文件
    assert list(tmp_path.glob("records-*.jsonl")) == []


async def test_aclose_is_noop(tmp_path: Path) -> None:
    """aclose() 不再做任何事（落盘由 flush() 负责）"""
    r = Recorder(tmp_path, batch_size=10)
    await r.aclose()
    # 不应抛异常


# ============================================================
# 落盘格式
# ============================================================


async def test_jsonl_one_record_per_line(tmp_path: Path) -> None:
    r = Recorder(tmp_path, batch_size=10)
    r.enqueue(_make_record(1))
    await r.flush()
    files = list(tmp_path.glob("records-*.jsonl"))
    assert len(files) == 1
    line = files[0].read_text().strip()
    obj = json.loads(line)
    assert obj["record_id"] == "rec-0001"
    assert obj["service"] == "svc"
    assert obj["request"]["model"] == "gpt-4o"


async def test_jsonl_filename_uses_date(tmp_path: Path) -> None:
    r = Recorder(tmp_path, batch_size=10)
    r.enqueue(_make_record(1))
    await r.flush()
    files = list(tmp_path.glob("records-*.jsonl"))
    assert len(files) == 1
    # 文件名形如 records-YYYY-MM-DD.jsonl
    assert re.match(r"records-\d{4}-\d{2}-\d{2}\.jsonl", files[0].name)
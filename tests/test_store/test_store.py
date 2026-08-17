"""Store（SQLite 检测结果库）测试"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from saitec.core.models import DetectionResult
from saitec.store.store import Store


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """每个测试一个临时 SQLite 文件（隔离 + Windows 兼容）"""
    return tmp_path / "test.db"


@pytest.fixture
def sample_result() -> DetectionResult:
    return DetectionResult(
        record_id="rec-001",
        service="openai-chat-completions",
        endpoint_type="openai-chat-completions",
        upstream="https://api.openai.com",
        timestamp="2026-08-14T12:00:00Z",
        status_code=200,
        elapsed_ms=812,
        model="gpt-4o",
        prompt_tokens=12,
        completion_tokens=34,
        finish_reason="stop",
        detection_status="clean",
        risk_level="low",
        detection_detail={"score": 0.1, "reason": "ok"},
        detected_at="2026-08-14T12:00:01Z",
    )


# ============================================================
# 构造与 schema
# ============================================================


def test_store_creates_schema(db_path: Path) -> None:
    Store(db_path)
    # 直接打开 SQLite 文件验证表已创建
    with sqlite3.connect(db_path) as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "detection_results" in tables
    assert "report_cursor" in tables
    assert "schema_version" in tables


def test_store_wal_mode(db_path: Path) -> None:
    Store(db_path)
    with sqlite3.connect(db_path) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_store_idempotent_init(db_path: Path) -> None:
    """重复构造 Store 不应出错"""
    Store(db_path)  # noqa
    Store(db_path)  # noqa
    # schema_version 应只有一行
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT version FROM schema_version").fetchall()
    assert len(rows) == 1


# ============================================================
# save_results
# ============================================================


async def test_save_and_query_one(db_path: Path, sample_result: DetectionResult) -> None:
    store = Store(db_path)
    await store.save_results([sample_result])
    results = await store.query(
        since=datetime(2026, 8, 14, 11, 0, 0),
    )
    assert len(results) == 1
    r = results[0]
    assert r.record_id == "rec-001"
    assert r.service == "openai-chat-completions"
    assert r.elapsed_ms == 812
    assert r.detection_detail == {"score": 0.1, "reason": "ok"}


async def test_save_idempotent(db_path: Path, sample_result: DetectionResult) -> None:
    """重复保存应去重（record_id UNIQUE）"""
    store = Store(db_path)
    await store.save_results([sample_result])
    await store.save_results([sample_result])  # 第二次
    results = await store.query(since=datetime(2026, 8, 14))
    assert len(results) == 1


async def test_save_empty_list(db_path: Path) -> None:
    """空列表不报错且不产生写入"""
    store = Store(db_path)
    await store.save_results([])  # 不应崩


async def test_query_filter_by_service(db_path: Path) -> None:
    store = Store(db_path)
    await store.save_results(
        [
            DetectionResult(
                record_id="r1",
                service="svc-a",
                endpoint_type="openai-chat-completions",
                upstream="https://api.openai.com",
                timestamp="2026-08-14T10:00:00Z",
                status_code=200,
                elapsed_ms=100,
                detected_at="2026-08-14T10:00:01Z",
            ),
            DetectionResult(
                record_id="r2",
                service="svc-b",
                endpoint_type="anthropic-messages",
                upstream="https://api.anthropic.com",
                timestamp="2026-08-14T10:01:00Z",
                status_code=200,
                elapsed_ms=200,
                detected_at="2026-08-14T10:01:01Z",
            ),
        ]
    )
    results = await store.query(since=datetime(2026, 8, 14), service="svc-a")
    assert len(results) == 1
    assert results[0].service == "svc-a"
    assert results[0].record_id == "r1"


async def test_query_filter_by_since(db_path: Path) -> None:
    store = Store(db_path)
    await store.save_results(
        [
            DetectionResult(
                record_id="old",
                service="svc",
                endpoint_type="openai-chat-completions",
                upstream="https://api.openai.com",
                timestamp="2026-08-13T00:00:00Z",
                status_code=200,
                elapsed_ms=100,
                detected_at="2026-08-13T00:00:01Z",
            ),
            DetectionResult(
                record_id="new",
                service="svc",
                endpoint_type="openai-chat-completions",
                upstream="https://api.openai.com",
                timestamp="2026-08-14T00:00:00Z",
                status_code=200,
                elapsed_ms=100,
                detected_at="2026-08-14T00:00:01Z",
            ),
        ]
    )
    results = await store.query(since=datetime(2026, 8, 14))
    assert len(results) == 1
    assert results[0].record_id == "new"


async def test_query_limit(db_path: Path) -> None:
    store = Store(db_path)
    results_in = [
        DetectionResult(
            record_id=f"r{i}",
            service="svc",
            endpoint_type="openai-chat-completions",
            upstream="https://api.openai.com",
            timestamp=f"2026-08-14T10:00:{i:02d}Z",
            status_code=200,
            elapsed_ms=100,
            detected_at=f"2026-08-14T10:00:{i:02d}Z",
        )
        for i in range(10)
    ]
    await store.save_results(results_in)
    results = await store.query(since=datetime(2026, 8, 14), limit=5)
    assert len(results) == 5


# ============================================================
# cursor
# ============================================================


async def test_cursor_initial_state(db_path: Path) -> None:
    store = Store(db_path)
    cur = await store.get_cursor()
    assert cur.last_record_id is None
    assert cur.last_timestamp is None
    assert cur.updated_at == "1970-01-01T00:00:00Z"


async def test_cursor_advance(db_path: Path) -> None:
    from saitec.core.models import ReportCursor

    store = Store(db_path)
    new = ReportCursor(
        last_record_id="rec-100",
        last_timestamp="2026-08-14T10:00:00Z",
        updated_at="2026-08-14T10:00:01Z",
    )
    await store.advance_cursor(new)
    cur = await store.get_cursor()
    assert cur.last_record_id == "rec-100"
    assert cur.last_timestamp == "2026-08-14T10:00:00Z"
    assert cur.updated_at == "2026-08-14T10:00:01Z"


async def test_cursor_advance_then_save_idempotent(
    db_path: Path, sample_result: DetectionResult
) -> None:
    """游标推进后再次保存应保持幂等"""
    from saitec.core.models import ReportCursor

    store = Store(db_path)
    await store.advance_cursor(
        ReportCursor(
            last_record_id="rec-001",
            last_timestamp="2026-08-14T12:00:00Z",
            updated_at="2026-08-14T12:00:01Z",
        )
    )
    await store.save_results([sample_result])
    # query 应仍然返回 1 条（去重）
    results = await store.query(since=datetime(2026, 8, 14))
    assert len(results) == 1


# ============================================================
# 检测状态枚举校验（DB 层）
# ============================================================


async def test_invalid_detection_status_rejected(db_path: Path) -> None:
    """DB 用 CHECK 约束阻止非法 detection_status"""
    store = Store(db_path)
    bad = DetectionResult(
        record_id="r",
        service="svc",
        endpoint_type="openai-chat-completions",
        upstream="https://api.openai.com",
        timestamp="2026-08-14T10:00:00Z",
        status_code=200,
        elapsed_ms=100,
        detection_status="totally-invalid",  # 不在枚举内
        detected_at="2026-08-14T10:00:01Z",
    )
    with pytest.raises(sqlite3.IntegrityError):
        await store.save_results([bad])
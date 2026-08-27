"""Store — Layer 2

SQLite 检测结果持久化（WAL 模式 + `busy_timeout`）。

⚠️ 骨架阶段已实装。SQLite 是同步 API（asyncio 直接调用，事件循环会被
轻微阻塞但单机自用场景足够）。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..core.models import DetectionResult, ReportCursor


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS detection_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id TEXT NOT NULL UNIQUE,
    service TEXT NOT NULL,
    endpoint_type TEXT NOT NULL,
    upstream TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    model TEXT,
    request_excerpt TEXT,
    response_excerpt TEXT,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    status_code INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL,
    finish_reason TEXT,
    detection_status TEXT NOT NULL CHECK (detection_status IN ('clean', 'suspicious', 'violation', 'error')),
    risk_level TEXT CHECK (risk_level IN ('low', 'medium', 'high', 'critical')),
    detection_detail TEXT,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_timestamp ON detection_results (timestamp);
CREATE INDEX IF NOT EXISTS idx_service_timestamp ON detection_results (service, timestamp);
CREATE INDEX IF NOT EXISTS idx_detection_status ON detection_results (detection_status, timestamp);
CREATE INDEX IF NOT EXISTS idx_risk_level ON detection_results (risk_level, timestamp);

CREATE TABLE IF NOT EXISTS report_cursor (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_record_id TEXT,
    last_timestamp TEXT,
    updated_at TEXT NOT NULL
);

INSERT OR IGNORE INTO report_cursor (id, last_record_id, last_timestamp, updated_at)
VALUES (1, NULL, NULL, '1970-01-01T00:00:00Z');

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
"""

_INSERT_RESULT_SQL = """
INSERT INTO detection_results (
    record_id, service, endpoint_type, upstream, timestamp, detected_at,
    model, prompt_tokens, completion_tokens,
    status_code, latency_ms, finish_reason,
    detection_status, risk_level, detection_detail, error
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(record_id) DO UPDATE SET
    detection_status = excluded.detection_status,
    risk_level = excluded.risk_level,
    detection_detail = excluded.detection_detail,
    detected_at = excluded.detected_at,
    error = excluded.error
"""

_QUERY_RESULTS_BASE = """
SELECT
    record_id, service, endpoint_type, upstream, timestamp, detected_at,
    model, prompt_tokens, completion_tokens,
    status_code, latency_ms, finish_reason,
    detection_status, risk_level, detection_detail, error
FROM detection_results
WHERE timestamp >= ?
"""

_CURSOR_SELECT_SQL = "SELECT last_record_id, last_timestamp, updated_at FROM report_cursor WHERE id = 1"
_CURSOR_UPDATE_SQL = """
UPDATE report_cursor
SET last_record_id = ?, last_timestamp = ?, updated_at = ?
WHERE id = 1
"""


class Store:
    """SQLite 检测结果库

    启用 WAL 模式 + `busy_timeout`，避免多写一读时 `database is locked`。
    """

    def __init__(
        self,
        db_path: Path,
        busy_timeout_ms: int = 5000,
    ) -> None:
        self._db_path = db_path
        self._busy_timeout_ms = busy_timeout_ms
        # 初始化 schema（首次启动时建表）
        self._init_schema()

    # ============================================================
    # 内部辅助
    # ============================================================

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._db_path,
            timeout=self._busy_timeout_ms / 1000.0,
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        applied_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._connect() as conn:
            conn.executescript(_SCHEMA_SQL)
            conn.execute(
                "INSERT OR IGNORE INTO schema_version (version, applied_at) VALUES (?, ?)",
                (1, applied_at),
            )
            conn.commit()

    @staticmethod
    def _result_to_model(row: sqlite3.Row) -> DetectionResult:
        detail_raw = row["detection_detail"]
        detail = json.loads(detail_raw) if detail_raw else None
        return DetectionResult(
            record_id=row["record_id"],
            service=row["service"],
            endpoint_type=row["endpoint_type"],
            upstream=row["upstream"],
            timestamp=row["timestamp"],
            status_code=row["status_code"],
            elapsed_ms=row["latency_ms"],
            model=row["model"],
            prompt_tokens=row["prompt_tokens"],
            completion_tokens=row["completion_tokens"],
            finish_reason=row["finish_reason"],
            error=row["error"],
            detection_status=row["detection_status"],
            risk_level=row["risk_level"],
            detection_detail=detail,
            detected_at=row["detected_at"],
        )

    # ============================================================
    # 公开 API
    # ============================================================

    async def save_results(self, results: list[DetectionResult]) -> None:
        """写入检测结果（按 `record_id` UNIQUE 约束去重）

        使用 `INSERT OR IGNORE`，重复上报安全（幂等）。
        """
        if not results:
            return
        with self._connect() as conn:
            for r in results:
                detail_json = (
                    json.dumps(r.detection_detail)
                    if r.detection_detail is not None
                    else None
                )
                conn.execute(
                    _INSERT_RESULT_SQL,
                    (
                        r.record_id,
                        r.service,
                        r.endpoint_type,
                        r.upstream,
                        r.timestamp,
                        r.detected_at,
                        r.model,
                        r.prompt_tokens,
                        r.completion_tokens,
                        r.status_code,
                        r.elapsed_ms,
                        r.finish_reason,
                        r.detection_status,
                        r.risk_level,
                        detail_json,
                        r.error,
                    ),
                )
            conn.commit()

    async def query(
        self,
        since: datetime,
        service: str | None = None,
        limit: int = 100,
        status: list[str] | None = None,
    ) -> list[DetectionResult]:
        """查询检测结果（按时间倒序）

        Args:
            since: 起始时间
            service: 可选，按服务名过滤
            limit: 返回结果数上限
            status: 可选，按 detection_status 过滤（SQL 层 IN——先 limit 后过滤会漏数据）
        """
        since_iso = since.isoformat()
        clauses: list[str] = []
        params: list[object] = [since_iso]
        if service is not None:
            clauses.append("AND service = ?")
            params.append(service)
        if status:
            placeholders = ", ".join("?" for _ in status)
            clauses.append(f"AND detection_status IN ({placeholders})")
            params.extend(status)
        tail = (" " + " ".join(clauses) if clauses else "") + " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(_QUERY_RESULTS_BASE + tail, params).fetchall()
        return [self._result_to_model(r) for r in rows]

    async def get_cursor(self) -> ReportCursor:
        """读取上报游标（用于断点续传）"""
        with self._connect() as conn:
            row = conn.execute(_CURSOR_SELECT_SQL).fetchone()
        if row is None:
            return ReportCursor()
        return ReportCursor(
            last_record_id=row["last_record_id"],
            last_timestamp=row["last_timestamp"],
            updated_at=row["updated_at"],
        )

    async def advance_cursor(self, cursor: ReportCursor) -> None:
        """更新上报游标为最新位置"""
        with self._connect() as conn:
            conn.execute(
                _CURSOR_UPDATE_SQL,
                (
                    cursor.last_record_id,
                    cursor.last_timestamp,
                    cursor.updated_at,
                ),
            )
            conn.commit()
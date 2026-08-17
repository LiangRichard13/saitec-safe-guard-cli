"""Store — Layer 2

SQLite 检测结果持久化（WAL 模式 + `busy_timeout`）。

⚠️ 骨架阶段：接口已定义，内部逻辑待 Phase D 落地。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..core.models import DetectionResult, ReportCursor


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

    async def save_results(self, results: list[DetectionResult]) -> None:
        """写入检测结果（按 `record_id` UNIQUE 约束去重）"""
        raise NotImplementedError("Phase D 实现")

    async def query(
        self,
        since: datetime,
        service: str | None = None,
        limit: int = 100,
    ) -> list[DetectionResult]:
        """查询检测结果"""
        raise NotImplementedError("Phase D 实现")

    async def get_cursor(self) -> ReportCursor:
        """读取上报游标（用于断点续传）"""
        raise NotImplementedError("Phase D 实现")

    async def advance_cursor(self, cursor: ReportCursor) -> None:
        """更新上报游标"""
        raise NotImplementedError("Phase D 实现")
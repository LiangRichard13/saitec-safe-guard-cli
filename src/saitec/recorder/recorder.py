"""Recorder — Layer 2

归一化记录收集器：内存队列 + JSONL 落盘（按天分片）。

⚠️ 实现要点：
- `enqueue` 同步，线程/协程安全
- `flush` 异步从内存队列取一拨出 **并落盘**（不读 JSONL）
- 内存队列上限 `max_queue_size`，溢出时丢弃最旧并告警
- JSONL 是崩溃恢复源：进程崩溃后下次启动从 `report_cursor` 之后读 JSONL 重放
- 落盘绑定到 `flush()`，由 runtime 周期循环调用（**不**有独立后台 flush_loop，
  否则会重复写同一批）
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from datetime import date
from pathlib import Path

from ..core.models import Record

logger = logging.getLogger(__name__)


class Recorder:
    """归一化记录收集器

    详见 `docs/design/architecture.md` §4 Layer 2。
    """

    def __init__(
        self,
        records_dir: Path,
        batch_size: int = 100,
        max_queue_size: int = 10000,
    ) -> None:
        self._records_dir = Path(records_dir)
        self._batch_size = batch_size
        self._max_queue_size = max_queue_size

        self._queue: list[Record] = []
        self._lock = asyncio.Lock()
        self._dropped_count = 0

    # ============================================================
    # 同步 / 异步 API
    # ============================================================

    def enqueue(self, record: Record) -> None:
        """同步入队（线程 / 协程安全）。

        溢出时丢弃**最旧**记录并累加 `_dropped_count`，**不抛异常**。
        """
        self._queue.append(record)
        overflow = len(self._queue) - self._max_queue_size
        if overflow > 0:
            dropped = self._queue[:overflow]
            del self._queue[:overflow]
            self._dropped_count += len(dropped)
            logger.warning(
                "recorder queue overflow, dropped %d record(s); total dropped=%d",
                len(dropped),
                self._dropped_count,
            )

    async def flush(self) -> list[Record]:
        """异步从内存队列取一拨出 + 同步落盘到 JSONL

        返回的 batch 由调用方（runtime）提交给 reporter。
        剩余不足 `batch_size` 的也一并返回，方便 drain。
        """
        async with self._lock:
            if not self._queue:
                return []
            batch = self._queue[: self._batch_size]
            self._queue = self._queue[self._batch_size :]
        # 落盘（不持有锁，避免阻塞 enqueue）
        self._append_to_jsonl(batch)
        return batch

    async def aclose(self) -> None:
        """无后台任务的优雅关闭（no-op，但保持接口一致）"""
        # 已没有后台任务，此方法保留接口签名
        return

    def queue_depth(self) -> int:
        """当前内存队列深度（给 `status` 命令用）"""
        return len(self._queue)

    def dropped_count(self) -> int:
        """自启动以来累计丢弃的记录数（队列溢出 / 关闭期等）"""
        return self._dropped_count

    # ============================================================
    # JSONL 落盘
    # ============================================================

    def _today_path(self) -> Path:
        return self._records_dir / f"records-{date.today().isoformat()}.jsonl"

    def _append_to_jsonl(self, records: list[Record]) -> None:
        """同步追加记录到 JSONL 文件"""
        self._records_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._today_path()
        with open(path, "a", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(dataclasses.asdict(r), ensure_ascii=False))
                f.write("\n")
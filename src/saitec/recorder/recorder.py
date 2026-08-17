"""Recorder — Layer 2

接收 proxy 的归一化记录，写内存队列 + 异步追加 JSONL 落盘。

⚠️ 骨架阶段：接口已定义，内部逻辑待 Phase B / Phase D 落地。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from ..core.models import Record


class Recorder:
    """归一化记录收集器

    详见 `docs/design/architecture.md` §4 Layer 2。
    """

    def __init__(
        self,
        queue_path: Path,
        batch_size: int = 100,
        max_queue_size: int = 10000,
    ) -> None:
        self._queue_path = queue_path
        self._batch_size = batch_size
        self._max_queue_size = max_queue_size
        self._lock = asyncio.Lock()
        self._queue: list[Record] = []

    def enqueue(self, record: Record) -> None:
        """同步入队（线程 / 协程安全，由 `_lock` 保护）"""
        raise NotImplementedError("Phase D 实现")

    async def flush(self) -> list[Record]:
        """异步从内存队列取一批出（**不**读 JSONL）"""
        raise NotImplementedError("Phase D 实现")

    async def aclose(self) -> None:
        """优雅关闭：等待内存队列 + 落盘 flush"""
        raise NotImplementedError("Phase D 实现")

    def queue_depth(self) -> int:
        """当前内存队列深度（给 `status` 命令用）"""
        raise NotImplementedError("Phase D 实现")
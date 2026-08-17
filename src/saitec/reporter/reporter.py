"""Reporter — Layer 2

HTTP POST 批量上报到检测服务器，含错误类型区分（AUTH / PAYLOAD / SERVER）。

⚠️ 骨架阶段：接口已定义，内部逻辑待 Phase D 落地。
"""
from __future__ import annotations

from enum import Enum

import aiohttp

from ..core.models import DetectorConfig, DetectionResult, Record


class ReportErrorKind(str, Enum):
    """上报错误类型（runtime 据此决策重试策略）"""

    AUTH = "AUTH"
    PAYLOAD = "PAYLOAD"
    SERVER = "SERVER"


class ReportError(Exception):
    """上报失败（含错误分类）"""

    def __init__(self, kind: ReportErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


class Reporter:
    """HTTP 上报到检测服务器，带 `X-API-Key` 认证"""

    def __init__(
        self,
        config: DetectorConfig,
        client: aiohttp.ClientSession,
    ) -> None:
        self._config = config
        self._client = client

    async def report(self, batch: list[Record]) -> list[DetectionResult]:
        """批量上报，返回检测结果"""
        raise NotImplementedError("Phase D 实现")
"""Runtime — Layer 5

唯一的编排者。持有所有层实例；负责启动、停止、定时任务、状态汇总。

详见 `docs/design/architecture.md` §4 Layer 5。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from ..adapters import get_adapter
from ..core.models import (
    AppConfig,
    ConfigSources,
    DetectionResult,
    EndpointSpec,
)
from ..proxy.server import ProxyService
from ..recorder.recorder import Recorder
from ..reporter.reporter import Reporter
from ..store.store import Store


class Runtime:
    """运行时编排器"""

    def __init__(self, config: AppConfig, sources: ConfigSources) -> None:
        self._config = config
        self._sources = sources
        self._stopped = True
        self._auth_failed = False

        self._recorder: Recorder | None = None
        self._reporter: Reporter | None = None
        self._store: Store | None = None
        self._proxies: list[ProxyService] = []

    @staticmethod
    def build_from(
        config_path: Path | None = None,
        **cli_overrides: Any,
    ) -> "Runtime":
        """工厂方法：执行配置三级加载 + 校验 + 构造 Runtime

        顺序：`config.json` → env → cli → validate。
        """
        raise NotImplementedError("Phase D 实现")

    async def start(self) -> None:
        """启动所有 ProxyService + 后台上报任务"""
        raise NotImplementedError("Phase D 实现")

    async def stop(self) -> None:
        """优雅停止：拒绝新请求 + flush 队列 + 关闭各层"""
        raise NotImplementedError("Phase D 实现")

    async def status(self) -> dict[str, Any]:
        """查询运行状态（各端口 / 队列 / 上报状态）"""
        raise NotImplementedError("Phase D 实现")

    async def query_results(
        self,
        since: datetime,
        service: str | None = None,
        limit: int = 100,
    ) -> list[DetectionResult]:
        """查询 SQLite 检测结果"""
        raise NotImplementedError("Phase D 实现")

    @property
    def config(self) -> AppConfig:
        return self._config

    @property
    def sources(self) -> ConfigSources:
        return self._sources

    @property
    def auth_failed(self) -> bool:
        return self._auth_failed
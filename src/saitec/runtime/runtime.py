"""Runtime — Layer 5

唯一的编排者。持有所有层实例；负责启动、停止、定时任务、状态汇总。

详见 `docs/design/architecture.md` §4 Layer 5 + §7.3。
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp

from ..adapters import get_adapter
from ..core.config import (
    apply_cli_overrides,
    apply_env_overrides,
    load_config_json,
    load_config_with_overrides,
    validate_config,
)
from ..core.models import (
    AppConfig,
    ConfigSources,
    ConfigValidationError,
    Record,
    ReportCursor,
)
from ..core.paths import resolve_config_path, resolve_data_dir
from ..core.utils import now_iso8601
from ..proxy.server import ProxyService
from ..recorder.recorder import Recorder
from ..reporter.reporter import ReportError, ReportErrorKind, Reporter
from ..store.store import Store

logger = logging.getLogger(__name__)


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
        self._http_session: aiohttp.ClientSession | None = None
        self._proxies: list[ProxyService] = []

        self._report_task: asyncio.Task[None] | None = None
        self._cursor: ReportCursor | None = None

    # ============================================================
    # 工厂方法
    # ============================================================

    @staticmethod
    def build_from(
        config_path: Path | None = None,
        **cli_overrides: Any,
    ) -> "Runtime":
        """工厂方法：三级加载 + 校验 + 构造 Runtime（不启动 IO）

        Raises:
            FileNotFoundError: config.json 不存在
            ValueError: JSON 损坏
            ConfigValidationError: 校验失败
        """
        if config_path is None:
            config_path = resolve_config_path()
        config, sources = load_config_with_overrides(config_path, **cli_overrides)
        return Runtime(config, sources)

    # ============================================================
    # 生命周期
    # ============================================================

    async def start(self) -> None:
        if not self._stopped:
            return
        try:
            data_dir = resolve_data_dir()
            data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

            # 1. Recorder（JSONL 落盘）
            records_dir = data_dir / "records"
            records_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._recorder = Recorder(
                records_dir,
                batch_size=self._config.detector.batch_size,
                max_queue_size=self._config.detector.max_queue_size,
            )

            # 2. Store（SQLite 检测结果）
            db_path = data_dir / "results.db"
            self._store = Store(db_path)

            # 3. HTTP session + Reporter
            self._http_session = aiohttp.ClientSession()
            self._reporter = Reporter(self._config.detector, self._http_session)

            # 4. ProxyService（每个 service 一个）
            for spec in self._config.services:
                adapter = get_adapter(spec.endpoint_type)
                proxy = ProxyService(
                    spec, adapter, self._recorder, self._http_session
                )
                await proxy.start()
                self._proxies.append(proxy)

            # 5. 加载上报游标 + 启动后台任务
            self._cursor = await self._store.get_cursor()
            self._stopped = False
            self._report_task = asyncio.create_task(
                self._report_loop(), name="runtime-report-loop"
            )
            logger.info(
                "runtime started: %d service(s), detector=%s",
                len(self._proxies),
                self._config.detector.url,
            )
        except Exception:
            # 启动失败：清理已分配资源
            await self._cleanup_partial()
            raise

    async def stop(self) -> None:
        """优雅停止"""
        self._stopped = True
        if self._report_task is not None:
            self._report_task.cancel()
            try:
                await self._report_task
            except asyncio.CancelledError:
                pass
            self._report_task = None
        # 关闭 ProxyService
        for proxy in self._proxies:
            try:
                await proxy.stop()
            except Exception:
                logger.exception("proxy stop failed: %s", proxy._spec.name)  # noqa: SLF001
        self._proxies = []
        # 最后一次 flush + 上报（如果有数据）
        await self._final_flush_and_report()
        # 关闭 HTTP session
        if self._http_session is not None:
            await self._http_session.close()
            self._http_session = None
        logger.info("runtime stopped")

    async def _cleanup_partial(self) -> None:
        """start() 中途失败时的清理"""
        for proxy in self._proxies:
            try:
                await proxy.stop()
            except Exception:
                pass
        self._proxies = []
        if self._http_session is not None:
            await self._http_session.close()
            self._http_session = None

    async def _final_flush_and_report(self) -> None:
        """停止前的最后 flush + 上报（确保内存数据不丢）"""
        if self._recorder is None or self._reporter is None or self._store is None:
            return
        try:
            batch = await self._recorder.flush()
            if not batch:
                return
            results = await self._reporter.report(batch)
            await self._store.save_results(results)
            await self._store.advance_cursor(self._make_cursor(batch[-1]))
        except Exception:
            logger.exception("final flush + report failed")

    # ============================================================
    # 后台循环
    # ============================================================

    async def _report_loop(self) -> None:
        """周期上报循环（含错误分类 + 指数退避）"""
        # 先启动续传（recover 未上报记录）
        await self._replay_unreported()

        backoff = 1
        while not self._stopped:
            # 睡眠（指数退避在错误时使用，所以正常情况 sleep 是 report_interval_sec）
            sleep_sec = self._config.detector.report_interval_sec if backoff == 1 else min(
                60, 2 ** backoff
            )
            try:
                await asyncio.sleep(sleep_sec)
            except asyncio.CancelledError:
                break
            if self._stopped:
                break
            try:
                batch = await self._recorder.flush()  # type: ignore[union-attr]
            except Exception:
                logger.exception("recorder flush failed")
                continue
            if not batch:
                backoff = 1
                continue
            try:
                results = await self._reporter.report(batch)  # type: ignore[union-attr]
                await self._store.save_results(results)  # type: ignore[union-attr]
                await self._store.advance_cursor(self._make_cursor(batch[-1]))
                backoff = 1
            except ReportError as e:
                if e.kind == ReportErrorKind.AUTH:
                    self._auth_failed = True
                    logger.error("X-API-Key 失效，停止上报：请重新 init")
                    return
                # PAYLOAD / SERVER：指数退避
                logger.warning(
                    "report failed (kind=%s): %s; backoff=%ds",
                    e.kind, e.message, min(60, 2 ** backoff),
                )
                backoff = min(backoff + 1, 6)

    async def _replay_unreported(self) -> None:
        """启动续传：从 JSONL 读出 cursor 之后的记录，重报"""
        if self._recorder is None or self._reporter is None or self._store is None:
            return
        if self._cursor is None:
            return
        cursor = self._cursor
        if cursor.last_record_id is None and cursor.last_timestamp is None:
            return  # 首次启动，无需续传
        records_dir = self._recorder._records_dir  # noqa: SLF001
        if not records_dir.exists():
            return

        for jsonl_file in sorted(records_dir.glob("records-*.jsonl")):
            try:
                with open(jsonl_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec_data = json.loads(line)
                        except json.JSONDecodeError:
                            logger.warning("损坏的 JSONL 行已跳过: %s", jsonl_file)
                            continue
                        # 比较 (timestamp, record_id) 严格大于游标
                        rec_ts = rec_data.get("timestamp", "")
                        rec_id = rec_data.get("record_id", "")
                        if (rec_ts, rec_id) <= (cursor.last_timestamp, cursor.last_record_id):
                            continue
                        # 转为 Record 再上报
                        rec = Record(
                            record_id=rec_data["record_id"],
                            service=rec_data["service"],
                            endpoint_type=rec_data["endpoint_type"],
                            upstream=rec_data["upstream"],
                            path=rec_data["path"],
                            timestamp=rec_data["timestamp"],
                            elapsed_ms=rec_data["elapsed_ms"],
                            status_code=rec_data["status_code"],
                            error=rec_data.get("error"),
                            request=rec_data.get("request", {}),
                            response=rec_data.get("response", {}),
                        )
                        try:
                            results = await self._reporter.report([rec])
                            await self._store.save_results(results)
                            await self._store.advance_cursor(
                                self._make_cursor(rec)
                            )
                        except ReportError as e:
                            if e.kind == ReportErrorKind.AUTH:
                                self._auth_failed = True
                                logger.error("续传时 auth 失败，停止续传")
                                return
                            logger.warning("续传单条失败: %s — %s", rec_id, e.message)
            except OSError:
                logger.exception("读 JSONL 失败: %s", jsonl_file)

    def _make_cursor(self, last_record: Record) -> ReportCursor:
        return ReportCursor(
            last_record_id=last_record.record_id,
            last_timestamp=last_record.timestamp,
            updated_at=now_iso8601(),
        )

    # ============================================================
    # 状态 / 查询
    # ============================================================

    async def status(self) -> dict[str, Any]:
        return {
            "running": not self._stopped,
            "auth_failed": self._auth_failed,
            "services": [p.status() for p in self._proxies],
            "queue_depth": self._recorder.queue_depth() if self._recorder else 0,
            "dropped_count": self._recorder.dropped_count() if self._recorder else 0,
            "config_sources": {
                "fields": dict(self._sources.sources),
                "env_vars": dict(self._sources.env_vars),
            },
        }

    async def query_results(
        self,
        since: datetime,
        service: str | None = None,
        limit: int = 100,
    ):
        if self._store is None:
            return []
        return await self._store.query(since, service=service, limit=limit)

    # ============================================================
    # 属性
    # ============================================================

    @property
    def config(self) -> AppConfig:
        return self._config

    @property
    def sources(self) -> ConfigSources:
        return self._sources

    @property
    def auth_failed(self) -> bool:
        return self._auth_failed
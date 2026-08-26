"""Runtime 集成测试

注意：Runtime 涉及多个子模块，用 mock upstream server + tmp_path 隔离
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import pytest
import pytest_asyncio

from saitec.adapters import get_adapter
from saitec.core.config import ConfigValidationError
from saitec.core.models import (
    AppConfig,
    ConfigSources,
    DetectorConfig,
    EndpointSpec,
    ReportCursor,
)
from saitec.core.paths import ensure_dirs
from saitec.runtime.runtime import Runtime


# ============================================================
# Fixture
# ============================================================


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """设置 SAITEC_CONFIG 到临时目录，写一份合法 config.json"""
    monkeypatch.setenv("SAITEC_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.delenv("SAITEC_API_KEY", raising=False)
    monkeypatch.delenv("SAITEC_DETECTOR_URL", raising=False)
    monkeypatch.delenv("SAITEC_LOG_LEVEL", raising=False)
    monkeypatch.delenv("SAITEC_REPORT_INTERVAL", raising=False)

    cfg = {
        "detector": {
            "url": "http://127.0.0.1:65500",  # 测试时换为 mock URL
            "api_key": "sk-test-key",
            "report_interval_sec": 1,
            "batch_size": 10,
            "max_queue_size": 100,
        },
        "services": [
            {
                "name": "svc-a",
                "port": 0,  # 自动分配
                "upstream": "http://127.0.0.1:65501",
                "endpoint_type": "openai-chat-completions",
                "record_body": True,
            }
        ],
        "log_level": "INFO",
    }
    (tmp_path / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    # Runtime 用 resolve_data_dir()，platformdirs 默认到系统目录。我们用 monkeypatch
    # 让 platformdirs 用 SAITEC_CONFIG 父目录。
    return tmp_path


@pytest_asyncio.fixture
async def mock_detector_server() -> tuple[str, web.AppRunner]:
    """Mock 检测服务器 + 返回 OK 响应"""
    from aiohttp import web

    runners: list[web.AppRunner] = []

    async def handle_detect(request: web.Request) -> web.Response:
        body = await request.json()
        batch = body.get("batch", [])
        results = [
            {
                "record_id": r["record_id"],
                "detection_status": "clean",
                "risk_level": "low",
                "detection_detail": {"score": 0.1},
                "detected_at": "2026-08-14T12:00:00Z",
            }
            for r in batch
        ]
        return web.json_response({"results": results})

    app = web.Application()
    app.router.add_post("/detect", handle_detect)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
    runners.append(runner)
    yield f"http://127.0.0.1:{port}", runner
    for r in runners:
        await r.cleanup()


# ============================================================
# build_from
# ============================================================


def test_build_from_loads_config(config_dir: Path) -> None:
    runtime = Runtime.build_from()
    assert isinstance(runtime, Runtime)
    assert runtime.config.detector.url == "http://127.0.0.1:65500"
    assert runtime.config.detector.api_key == "sk-test-key"
    assert len(runtime.config.services) == 1
    assert runtime.config.services[0].name == "svc-a"


def test_build_from_with_cli_override(config_dir: Path) -> None:
    runtime = Runtime.build_from(detector_url="http://override:9999")
    assert runtime.config.detector.url == "http://override:9999"


def test_build_from_validation_error(config_dir: Path) -> None:
    """apikey 空 → 校验失败"""
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "detector": {"url": "http://d", "api_key": ""},
                "services": [],
            }
        )
    )
    with pytest.raises(ConfigValidationError):
        Runtime.build_from()


def test_build_from_config_not_found(tmp_path: Path) -> None:
    """不存在的 config.json"""
    os.environ["SAITEC_CONFIG"] = str(tmp_path / "nope.json")
    try:
        with pytest.raises(FileNotFoundError):
            Runtime.build_from()
    finally:
        os.environ.pop("SAITEC_CONFIG", None)


# ============================================================
# start / stop / status
# ============================================================


@pytest.mark.asyncio
async def test_start_stop_lifecycle(config_dir: Path) -> None:
    """start 启动所有 IO 层 + stop 优雅关闭"""
    # 让 platformdirs 用我们临时目录（通过 SAITEC_CONFIG）
    runtime = Runtime.build_from()
    await runtime.start()
    try:
        status = await runtime.status()
        assert status["running"] is True
        assert status["auth_failed"] is False
        assert len(status["services"]) == 1
        svc = status["services"][0]
        assert svc["name"] == "svc-a"
        assert svc["running"] is True
        assert svc["endpoint_type"] == "openai-chat-completions"
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_stop_is_idempotent_safe(config_dir: Path) -> None:
    """第二次 stop 不崩"""
    runtime = Runtime.build_from()
    await runtime.start()
    await runtime.stop()
    await runtime.stop()  # 第二次：_proxies 已空，应不抛


@pytest.mark.asyncio
async def test_double_start_ignored(config_dir: Path) -> None:
    """连续两次 start 应幂等（不重建）"""
    runtime = Runtime.build_from()
    await runtime.start()
    proxies_first = list(runtime._proxies)  # noqa: SLF001
    await runtime.start()  # 第二次
    assert runtime._proxies == proxies_first  # noqa: SLF001
    await runtime.stop()


# ============================================================
# 真实端到端：proxy → recorder → _report_loop → store
# ============================================================


@pytest.mark.asyncio
async def test_end_to_end_records(
    config_dir: Path, mock_detector_server: tuple
) -> None:
    """端到端：客户端 → proxy → recorder → 周期上报 → store"""
    detector_url, _ = mock_detector_server
    # 把 config.json 里的 detector url 改到 mock detector
    cfg_path = config_dir / "config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["detector"]["url"] = detector_url
    cfg_path.write_text(json.dumps(cfg))

    runtime = Runtime.build_from()
    await runtime.start()
    try:
        # 提取实际 proxy 端口
        proxy_port = runtime._proxies[0]._actual_port  # noqa: SLF001
        local_url = f"http://127.0.0.1:{proxy_port}"

        # 客户端发请求（带 mock upstream 上游的 SSE 流）
        async with aiohttp.ClientSession() as session:
            await session.post(
                f"{local_url}/v1/chat/completions",
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
            # 等后台 _report_loop 周期上报（report_interval_sec=1）
            await asyncio.sleep(2)

        # 查询 SQLite 应有 1 条
        results = await runtime.query_results(
            since=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        assert len(results) == 1
        r = results[0]
        assert r.service == "svc-a"
        assert r.detection_status == "clean"
    finally:
        await runtime.stop()


# ============================================================
# 错误分类：AUTH vs SERVER
# ============================================================


@pytest.mark.asyncio
async def test_auth_error_stops_report_loop(config_dir: Path) -> None:
    """检测服务器返回 401 → _report_loop 停止 + auth_failed=True"""
    from aiohttp import web

    # mock 一个返回 401 的服务器
    runners: list[web.AppRunner] = []

    async def handle_401(request: web.Request) -> web.Response:
        return web.Response(status=401, text="unauthorized")

    app = web.Application()
    app.router.add_post("/detect", handle_401)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
    runners.append(runner)
    detector_url = f"http://127.0.0.1:{port}"

    try:
        cfg_path = config_dir / "config.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["detector"]["url"] = detector_url
        cfg["detector"]["report_interval_sec"] = 1
        cfg_path.write_text(json.dumps(cfg))

        runtime = Runtime.build_from()
        await runtime.start()
        try:
            proxy_port = runtime._proxies[0]._actual_port  # noqa: SLF001
            local_url = f"http://127.0.0.1:{proxy_port}"

            async with aiohttp.ClientSession() as session:
                await session.post(
                    f"{local_url}/v1/chat/completions",
                    json={"model": "gpt-4o", "messages": []},
                )

            # 等 _report_loop 自然周期触发 + 报告 → 401
            await asyncio.sleep(3)

            assert runtime.auth_failed is True
        finally:
            await runtime.stop()
    finally:
        for r in runners:
            await r.cleanup()


# ============================================================
# P0-4：上报失败记录不丢（server 先 500 后恢复 200）
# ============================================================


@pytest.mark.asyncio
async def test_report_failure_keeps_records(config_dir: Path) -> None:
    """检测服务器先 500 后 200：失败批应保留重试，最终全部到达 store（P0-4）"""
    from aiohttp import web

    call_count = 0

    async def handle_flaky(request: web.Request) -> web.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return web.Response(status=500, text="boom")
        body = await request.json()
        batch = body.get("batch", [])
        results = [
            {
                "record_id": r["record_id"],
                "detection_status": "clean",
                "risk_level": "low",
                "detected_at": "2026-08-14T12:00:00Z",
            }
            for r in batch
        ]
        return web.json_response({"results": results})

    app = web.Application()
    app.router.add_post("/detect", handle_flaky)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
    detector_url = f"http://127.0.0.1:{port}"

    try:
        cfg_path = config_dir / "config.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["detector"]["url"] = detector_url
        cfg["detector"]["report_interval_sec"] = 1
        cfg_path.write_text(json.dumps(cfg))

        runtime = Runtime.build_from()
        await runtime.start()
        try:
            proxy_port = runtime._proxies[0]._actual_port  # noqa: SLF001
            local_url = f"http://127.0.0.1:{proxy_port}"

            async with aiohttp.ClientSession() as session:
                await session.post(
                    f"{local_url}/v1/chat/completions",
                    json={"model": "gpt-4o", "messages": []},
                )
            # 等第一次触发（500 失败）+ 退避后重试（200 成功）
            # backoff: 失败 → 4s 后重试，另加 report_interval=1s
            await asyncio.sleep(7)

            results = await runtime.query_results(
                since=datetime.now(timezone.utc) - timedelta(minutes=5),
            )
            assert len(results) == 1  # 记录未因失败丢失
            assert call_count >= 2  # 至少失败一次 + 成功一次
        finally:
            await runtime.stop()
    finally:
        await runner.cleanup()


# ============================================================
# P0-5：意外异常不杀死 _report_loop（SQLite locked 等）
# ============================================================


@pytest.mark.asyncio
async def test_report_loop_survives_unexpected_exception(config_dir: Path) -> None:
    """store.save_results 抛非 ReportError 异常时，_report_loop 不崩，记录保留重试"""
    from aiohttp import web
    from unittest.mock import AsyncMock, patch

    call_count = 0

    async def handle_ok(request: web.Request) -> web.Response:
        nonlocal call_count
        call_count += 1
        body = await request.json()
        batch = body.get("batch", [])
        results = [
            {
                "record_id": r["record_id"],
                "detection_status": "clean",
                "risk_level": "low",
                "detected_at": "2026-08-14T12:00:00Z",
            }
            for r in batch
        ]
        return web.json_response({"results": results})

    app = web.Application()
    app.router.add_post("/detect", handle_ok)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
    detector_url = f"http://127.0.0.1:{port}"

    try:
        cfg_path = config_dir / "config.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["detector"]["url"] = detector_url
        cfg["detector"]["report_interval_sec"] = 1
        cfg_path.write_text(json.dumps(cfg))

        runtime = Runtime.build_from()

        # mock store.save_results 第一次抛意外异常，第二次正常
        save_call_count = 0
        original_save = None

        async def mock_save_results(results):
            nonlocal save_call_count
            save_call_count += 1
            if save_call_count == 1:
                raise RuntimeError("SQLite locked (模拟)")
            return await original_save(results)

        await runtime.start()
        try:
            original_save = runtime._store.save_results  # noqa: SLF001
            runtime._store.save_results = mock_save_results  # noqa: SLF001

            proxy_port = runtime._proxies[0]._actual_port  # noqa: SLF001
            local_url = f"http://127.0.0.1:{proxy_port}"

            async with aiohttp.ClientSession() as session:
                await session.post(
                    f"{local_url}/v1/chat/completions",
                    json={"model": "gpt-4o", "messages": []},
                )
            # 等第一次触发（save 抛异常）+ 退避后重试（save 成功）
            await asyncio.sleep(7)

            # _report_loop 应未崩溃，记录最终到达 SQLite
            results = await runtime.query_results(
                since=datetime.now(timezone.utc) - timedelta(minutes=5),
            )
            assert len(results) == 1
            assert save_call_count >= 2  # 第一次失败，第二次成功
        finally:
            await runtime.stop()
    finally:
        await runner.cleanup()


# ============================================================
# 事件钩子（monitor 用）
# ============================================================


@pytest.mark.asyncio
async def test_event_sink_traffic_and_report(config_dir: Path, mock_detector_server: tuple) -> None:
    """event_sink 收到 traffic（代理流量）与 report（上报结果）事件"""
    detector_url, _ = mock_detector_server
    cfg_path = config_dir / "config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["detector"]["url"] = detector_url
    cfg["detector"]["report_interval_sec"] = 1
    cfg_path.write_text(json.dumps(cfg))

    events: list[tuple[str, dict]] = []
    runtime = Runtime.build_from(event_sink=lambda k, p: events.append((k, p)))
    await runtime.start()
    try:
        proxy_port = runtime._proxies[0]._actual_port  # noqa: SLF001
        async with aiohttp.ClientSession() as session:
            await session.post(
                f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
                json={"model": "gpt-4o", "messages": []},
            )
        await asyncio.sleep(2.5)  # 等 1s 上报周期

        kinds = [k for k, _ in events]
        assert "started" in kinds
        assert "traffic" in kinds
        assert "report" in kinds
        traffic = next(p for k, p in events if k == "traffic")
        assert traffic["service"] == "svc-a"
        assert traffic["status_code"] in (200, 502)  # mock 上游不存在，两种都算流量事件
        rep = next(p for k, p in events if k == "report")
        assert rep["total"] >= 1
        assert isinstance(rep["flagged"], list)
    finally:
        await runtime.stop()
    assert "stopped" in [k for k, _ in events]


@pytest.mark.asyncio
async def test_event_sink_auth_failed(config_dir: Path) -> None:
    """detector 401 → auth_failed 事件"""
    from aiohttp import web

    async def handle_401(request: web.Request) -> web.Response:
        return web.Response(status=401, text="unauthorized")

    app = web.Application()
    app.router.add_post("/detect", handle_401)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
    try:
        cfg_path = config_dir / "config.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["detector"]["url"] = f"http://127.0.0.1:{port}"
        cfg["detector"]["report_interval_sec"] = 1
        cfg_path.write_text(json.dumps(cfg))

        events: list[tuple[str, dict]] = []
        runtime = Runtime.build_from(event_sink=lambda k, p: events.append((k, p)))
        await runtime.start()
        try:
            proxy_port = runtime._proxies[0]._actual_port  # noqa: SLF001
            async with aiohttp.ClientSession() as session:
                await session.post(
                    f"http://127.0.0.1:{proxy_port}/v1/chat/completions",
                    json={"model": "gpt-4o", "messages": []},
                )
            await asyncio.sleep(3)

            assert "auth_failed" in [k for k, _ in events]
            payload = next(p for k, p in events if k == "auth_failed")
            assert "message" in payload
        finally:
            await runtime.stop()
    finally:
        await runner.cleanup()


# ============================================================
# pending 队列不清空时不取新批（防内存无界增长）
# ============================================================


@pytest.mark.asyncio
async def test_report_loop_pending_blocks_new_batch(config_dir: Path) -> None:
    """pending 未清空时，_report_loop 不再 flush 新批"""
    from aiohttp import web

    call_count = 0

    async def handle_always_500(request: web.Request) -> web.Response:
        nonlocal call_count
        call_count += 1
        return web.Response(status=500, text="永远失败")

    app = web.Application()
    app.router.add_post("/detect", handle_always_500)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
    detector_url = f"http://127.0.0.1:{port}"

    try:
        cfg_path = config_dir / "config.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["detector"]["url"] = detector_url
        cfg["detector"]["report_interval_sec"] = 1
        cfg_path.write_text(json.dumps(cfg))

        runtime = Runtime.build_from()
        await runtime.start()
        try:
            proxy_port = runtime._proxies[0]._actual_port  # noqa: SLF001
            local_url = f"http://127.0.0.1:{proxy_port}"

            async with aiohttp.ClientSession() as session:
                # 发 2 条记录
                await session.post(
                    f"{local_url}/v1/chat/completions",
                    json={"model": "gpt-4o", "messages": []},
                )
                await asyncio.sleep(0.1)
                await session.post(
                    f"{local_url}/v1/chat/completions",
                    json={"model": "gpt-4o", "messages": []},
                )

            # 等足够时间触发多次周期：pending 阻塞后不应再 flush
            await asyncio.sleep(5)

            # recorder 队列应还有第二条（因为第一批失败后 pending 不空，不再取新批）
            # 或者第一批如果 flush 了 2 条，pending 里就是 2 条，也不会再 flush
            # 关键检查：call_count 应不会无限增长（pending 阻塞生效）
            assert call_count >= 2  # 至少失败 2 次（第一批 + 重试）
            assert call_count < 10  # 没有无限重试取新批（pending 阻塞生效）
        finally:
            await runtime.stop()
    finally:
        await runner.cleanup()


# ============================================================
# 续传 (_replay_unreported) 测试
# ============================================================


@pytest.mark.asyncio
async def test_replay_unreported_on_restart(
    config_dir: Path, mock_detector_server: tuple
) -> None:
    """进程重启后，_replay_unreported 自动续传游标之后的未上报记录"""
    from saitec.core.models import ReportCursor

    detector_url, _ = mock_detector_server
    cfg_path = config_dir / "config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg["detector"]["url"] = detector_url
    cfg["detector"]["report_interval_sec"] = 999  # 不让自动周期上报干扰，续传是焦点
    cfg_path.write_text(json.dumps(cfg))

    # 数据准备：config 目录下写 JSONL，含 2 条记录（r_old 在游标前，r_new 在游标后）
    records_dir = config_dir / "records"
    records_dir.mkdir(parents=True, exist_ok=True)

    def _mk_record(rid: str, ts: str) -> dict:
        return {
            "record_id": rid,
            "service": "svc-a",
            "endpoint_type": "openai-chat-completions",
            "upstream": "http://up",
            "path": "/v1/chat/completions",
            "timestamp": ts,
            "elapsed_ms": 100,
            "status_code": 200,
            "error": None,
            "request": {},
            "response": {"content": "x"},
        }

    ts_old = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    ts_new = datetime.now(timezone.utc).isoformat()
    jsonl = records_dir / "records-2026-08-20.jsonl"
    jsonl.write_text(
        json.dumps(_mk_record("r_old", ts_old)) + "\n"
        + json.dumps(_mk_record("r_new", ts_new)) + "\n",
        encoding="utf-8",
    )

    # 预先把游标推进到 r_old（模拟第一轮只上报到 r_old 就崩溃）
    config_db_backup = None
    runtime = Runtime.build_from()
    await runtime.start()
    try:
        store = runtime._store  # noqa: SLF001
        await store.advance_cursor(
            ReportCursor(
                last_record_id="r_old",
                last_timestamp=ts_old,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        # 清空 recorder 内存队列（避免自动上报反而把 r_new 报了）
        runtime._recorder._queue.clear()  # noqa: SLF001
    finally:
        await runtime.stop()

    # 第二轮：重启，_replay_unreported 应只续传 r_new（r_old 已被游标越过）
    runtime2 = Runtime.build_from()
    await runtime2.start()
    try:
        await asyncio.sleep(2)  # 等 _replay_unreported 跑完
        results = await runtime2.query_results(
            since=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        assert len(results) == 1  # 只续传了游标之后的 r_new
        assert results[0].record_id == "r_new"
        assert results[0].detection_status == "clean"
    finally:
        await runtime2.stop()
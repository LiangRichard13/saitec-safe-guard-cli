"""Reporter（HTTP 上报）测试

用手写 `aiohttp.web.AppRunner` 启动本地 mock 检测服务器（避免引入 pytest-aiohttp）。
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

from ssgc.core.models import DetectorConfig, Record
from ssgc.reporter.reporter import ReportError, ReportErrorKind, Reporter


# ============================================================
# Fixture：本地 mock 检测服务器（手写 aiohttp.web）
# ============================================================


@pytest_asyncio.fixture
async def mock_detector() -> Any:
    """返回固定检测响应的本地服务器"""
    return_url = None

    async def handle_detect(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "results": [
                    {
                        "record_id": "rec-0001",
                        "detection_status": "clean",
                        "risk_level": "low",
                        "detection_detail": {"score": 0.1},
                        "detected_at": "2026-08-14T12:00:01Z",
                    }
                ]
            }
        )

    app = web.Application()
    app.router.add_post("/detect", handle_detect)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
    base_url = f"http://127.0.0.1:{port}"
    try:
        yield base_url
    finally:
        await runner.cleanup()


@pytest_asyncio.fixture
async def status_server_factory() -> Callable[[int], Awaitable[str]]:
    """工厂 fixture：传入状态码，返回对应 mock URL"""
    runners: list[web.AppRunner] = []

    async def _make(status_code: int) -> str:
        async def handle_detect(request: web.Request) -> web.Response:
            return web.Response(status=status_code, text=f"status {status_code}")

        app = web.Application()
        app.router.add_post("/detect", handle_detect)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
        runners.append(runner)
        return f"http://127.0.0.1:{port}"

    yield _make
    for runner in runners:
        await runner.cleanup()


def _make_record(idx: int = 1) -> Record:
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
        response={
            "content": "hi",
            "usage": {"prompt_tokens": 12, "completion_tokens": 9},
            "finish_reason": "stop",
        },
    )


# ============================================================
# 正常路径
# ============================================================


async def test_report_ok(mock_detector: str) -> None:
    cfg = DetectorConfig(url=mock_detector, api_key="sk-test")
    async with aiohttp.ClientSession() as session:
        reporter = Reporter(cfg, session)
        results = await reporter.report([_make_record(1)])
    assert len(results) == 1
    r = results[0]
    assert r.record_id == "rec-0001"
    assert r.detection_status == "clean"
    assert r.risk_level == "low"
    assert r.service == "svc"
    assert r.prompt_tokens == 12
    assert r.completion_tokens == 9


async def test_report_empty_batch() -> None:
    cfg = DetectorConfig(url="http://x", api_key="k")
    async with aiohttp.ClientSession() as session:
        reporter = Reporter(cfg, session)
        assert await reporter.report([]) == []


async def test_report_custom_endpoint_path() -> None:
    """endpoint_path 自定义（如 /api/v1/detect-v2）时应打到该路径而非 /detect"""
    hits: list[str] = []

    async def handle_custom(request: web.Request) -> web.Response:
        hits.append(request.path)
        return web.json_response(
            {
                "results": [
                    {
                        "record_id": "rec-0001",
                        "detection_status": "clean",
                        "risk_level": "low",
                        "detected_at": "2026-08-14T12:00:01Z",
                    }
                ]
            }
        )

    app = web.Application()
    app.router.add_post("/api/v1/detect-v2", handle_custom)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
    base_url = f"http://127.0.0.1:{port}"
    try:
        cfg = DetectorConfig(
            url=base_url, api_key="sk-test", endpoint_path="/api/v1/detect-v2"
        )
        async with aiohttp.ClientSession() as session:
            reporter = Reporter(cfg, session)
            results = await reporter.report([_make_record(1)])
        assert len(results) == 1
        assert hits == ["/api/v1/detect-v2"]  # 打到了自定义路径
    finally:
        await runner.cleanup()


# ============================================================
# 错误分类
# ============================================================


@pytest.mark.parametrize("status_code", [401, 403])
async def test_report_auth_error(
    status_server_factory: Callable[[int], Awaitable[str]], status_code: int
) -> None:
    url = await status_server_factory(status_code)
    cfg = DetectorConfig(url=url, api_key="k")
    async with aiohttp.ClientSession() as session:
        reporter = Reporter(cfg, session)
        with pytest.raises(ReportError) as exc:
            await reporter.report([_make_record()])
    assert exc.value.kind == ReportErrorKind.AUTH


async def test_report_payload_error(
    status_server_factory: Callable[[int], Awaitable[str]]
) -> None:
    url = await status_server_factory(400)
    cfg = DetectorConfig(url=url, api_key="k")
    async with aiohttp.ClientSession() as session:
        reporter = Reporter(cfg, session)
        with pytest.raises(ReportError) as exc:
            await reporter.report([_make_record()])
    assert exc.value.kind == ReportErrorKind.PAYLOAD


@pytest.mark.parametrize("status_code", [500, 502, 503])
async def test_report_server_error(
    status_server_factory: Callable[[int], Awaitable[str]], status_code: int
) -> None:
    url = await status_server_factory(status_code)
    cfg = DetectorConfig(url=url, api_key="k")
    async with aiohttp.ClientSession() as session:
        reporter = Reporter(cfg, session)
        with pytest.raises(ReportError) as exc:
            await reporter.report([_make_record()])
    assert exc.value.kind == ReportErrorKind.SERVER


async def test_report_network_error() -> None:
    """连接被拒绝（无可用服务器）→ SERVER"""
    cfg = DetectorConfig(url="http://127.0.0.1:1", api_key="k")  # 不可达端口
    async with aiohttp.ClientSession() as session:
        reporter = Reporter(cfg, session, timeout_sec=1.0)
        with pytest.raises(ReportError) as exc:
            await reporter.report([_make_record()])
    assert exc.value.kind == ReportErrorKind.SERVER


# ============================================================
# 检测响应字段映射
# ============================================================


@pytest_asyncio.fixture
async def custom_response_server() -> Any:
    """返回特定 detection 响应的本地服务器"""
    runners: list[web.AppRunner] = []

    async def make_server(handler: Callable[[web.Request], Awaitable[web.Response]]) -> str:
        app = web.Application()
        app.router.add_post("/detect", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
        runners.append(runner)
        return f"http://127.0.0.1:{port}"

    yield make_server
    for runner in runners:
        await runner.cleanup()


async def test_response_field_mapping(custom_response_server: Any) -> None:
    """Record 字段正确合并到 DetectionResult"""

    async def handler(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "results": [
                    {
                        "record_id": "rec-0001",
                        "detection_status": "suspicious",
                        "risk_level": "high",
                        "detection_detail": {"reasons": ["prompt injection"]},
                        "detected_at": "2026-08-14T13:00:00Z",
                    }
                ]
            }
        )

    url = await custom_response_server(handler)
    cfg = DetectorConfig(url=url, api_key="k")
    async with aiohttp.ClientSession() as session:
        reporter = Reporter(cfg, session)
        results = await reporter.report([_make_record(1)])
    r = results[0]
    assert r.detection_status == "suspicious"
    assert r.risk_level == "high"
    assert r.detection_detail == {"reasons": ["prompt injection"]}
    # Record 字段合并
    assert r.service == "svc"
    assert r.endpoint_type == "openai-chat-completions"
    assert r.elapsed_ms == 100
    assert r.prompt_tokens == 12
    assert r.completion_tokens == 9


async def test_response_unknown_record_id_skipped(custom_response_server: Any) -> None:
    """响应中含未知 record_id 应被忽略（不崩）"""

    async def handler(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "results": [
                    {"record_id": "rec-0001", "detection_status": "clean"},
                    {"record_id": "rec-unknown", "detection_status": "clean"},
                ]
            }
        )

    url = await custom_response_server(handler)
    cfg = DetectorConfig(url=url, api_key="k")
    async with aiohttp.ClientSession() as session:
        reporter = Reporter(cfg, session)
        results = await reporter.report([_make_record(1)])
    assert len(results) == 1
    assert results[0].record_id == "rec-0001"
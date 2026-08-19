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
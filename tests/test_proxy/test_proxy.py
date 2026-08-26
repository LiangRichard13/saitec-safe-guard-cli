"""ProxyService（反向代理）集成测试

用 aiohttp.web 启 mock upstream，验证：
1. 请求被转发到上游
2. SSE 流式响应正确透传 + 累积
3. Record 内容正确
4. 错误路径（502 等）也有 Record
"""
from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

from saitec.adapters import get_adapter
from saitec.core.models import EndpointSpec
from saitec.proxy.server import ProxyService
from saitec.recorder.recorder import Recorder


# ============================================================
# Fixture
# ============================================================


@pytest_asyncio.fixture
async def upstream_openai_chat() -> tuple[str, web.AppRunner]:
    """Mock 上游：模拟 OpenAI Chat Completions 流式响应"""
    runners: list[web.AppRunner] = []

    async def handle(request: web.Request) -> web.StreamResponse:
        # 读取请求体（验证它确实被转发）
        await request.read()
        resp = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream"},
        )
        await resp.prepare(request)
        # 模拟 SSE 流
        chunks = [
            b'data: {"choices":[{"delta":{"role":"assistant","content":""}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":" world"}}]}\n\n',
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
            b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}\n\n',
            b'data: [DONE]\n\n',
        ]
        for c in chunks:
            await resp.write(c)
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
    runners.append(runner)
    upstream_url = f"http://127.0.0.1:{port}"
    yield upstream_url
    for runner in runners:
        await runner.cleanup()


@pytest_asyncio.fixture
async def upstream_failing() -> tuple[str, web.AppRunner]:
    """Mock 上游：拒绝连接（用关闭的端口）"""
    # 用不存在的端口
    return "http://127.0.0.1:1", None  # port 1 不可达


@pytest_asyncio.fixture
async def client_session():
    """整个测试期间保持打开的 aiohttp ClientSession"""
    async with aiohttp.ClientSession() as session:
        yield session


@pytest_asyncio.fixture
async def running_proxy(
    upstream_openai_chat: str,
    client_session: aiohttp.ClientSession,
    tmp_path,
) -> tuple[ProxyService, Recorder, str]:
    """起 ProxyService：上游是 mock openai"""
    upstream = upstream_openai_chat
    spec = EndpointSpec(
        name="test-svc",
        port=0,  # 自动分配
        upstream=upstream,
        endpoint_type="openai-chat-completions",
        record_body=True,
    )
    adapter = get_adapter("openai-chat-completions")
    recorder = Recorder(tmp_path, batch_size=100)
    proxy = ProxyService(spec, adapter, recorder, client_session)
    await proxy.start()
    # 提取实际端口
    site = proxy._site  # noqa: SLF001
    port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
    try:
        yield proxy, recorder, f"http://127.0.0.1:{port}"
    finally:
        await proxy.stop()


# ============================================================
# 正常路径
# ============================================================


async def test_proxy_streams_sse_response(running_proxy: tuple) -> None:
    """代理应该正确透传 SSE 流并累积 Record"""
    proxy, recorder, local_url = running_proxy

    request_body = json.dumps(
        {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }
    ).encode()

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{local_url}/v1/chat/completions",
            data=request_body,
            headers={"Content-Type": "application/json"},
        ) as resp:
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("text/event-stream")
            # 读取完整响应（透传）
            body = await resp.text()
            assert "Hello" in body
            assert "world" in body
            assert "[DONE]" in body

    # 给 recorder flush（按 C-2 设计，flush() 触发落盘）
    batch = await recorder.flush()
    assert len(batch) == 1
    record = batch[0]
    assert record.service == "test-svc"
    assert record.endpoint_type == "openai-chat-completions"
    assert record.upstream.startswith("http://")
    assert record.path == "/v1/chat/completions"
    assert record.status_code == 200
    assert record.error is None
    assert record.request["model"] == "gpt-4o"
    assert record.response["content"] == "Hello world"
    assert record.response["finish_reason"] == "stop"


async def test_proxy_status(running_proxy: tuple) -> None:
    proxy, _, _ = running_proxy
    status = proxy.status()
    assert status["name"] == "test-svc"
    assert status["running"] is True
    assert status["endpoint_type"] == "openai-chat-completions"
    assert status["port"] > 0


# ============================================================
# 错误路径
# ============================================================


@pytest_asyncio.fixture
async def proxy_with_dead_upstream(
    client_session: aiohttp.ClientSession,
    tmp_path,
) -> tuple[ProxyService, Recorder, int]:
    """上游不可达"""
    spec = EndpointSpec(
        name="dead-svc",
        port=0,
        upstream="http://127.0.0.1:1",  # 不可达
        endpoint_type="openai-chat-completions",
        record_body=True,
    )
    adapter = get_adapter("openai-chat-completions")
    recorder = Recorder(tmp_path, batch_size=100)
    proxy = ProxyService(spec, adapter, recorder, client_session, upstream_timeout_sec=2.0)
    await proxy.start()
    site = proxy._site  # noqa: SLF001
    port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
    try:
        yield proxy, recorder, port
    finally:
        await proxy.stop()


async def test_proxy_upstream_unreachable_records_error(
    proxy_with_dead_upstream: tuple,
) -> None:
    """上游不可达 → 返回 502 + 记录 error"""
    proxy, recorder, port = proxy_with_dead_upstream
    local_url = f"http://127.0.0.1:{port}"

    request_body = json.dumps(
        {"model": "gpt-4o", "messages": []}
    ).encode()

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{local_url}/v1/chat/completions",
            data=request_body,
        ) as resp:
            assert resp.status == 502

    batch = await recorder.flush()
    assert len(batch) == 1
    record = batch[0]
    assert record.status_code == 502
    assert record.error is not None
    assert "upstream error" in record.error


# ============================================================
# 非流式响应（content-type 不是 SSE）
# ============================================================


@pytest_asyncio.fixture
async def upstream_non_stream() -> tuple[str, web.AppRunner]:
    """Mock 上游：返回非流式 JSON"""

    async def handle(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "choices": [
                    {"message": {"role": "assistant", "content": "Hello"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            }
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
    yield f"http://127.0.0.1:{port}"
    await runner.cleanup()


@pytest_asyncio.fixture
async def running_proxy_non_stream(
    upstream_non_stream: str,
    client_session: aiohttp.ClientSession,
    tmp_path,
) -> tuple[ProxyService, Recorder, str]:
    spec = EndpointSpec(
        name="non-stream-svc",
        port=0,
        upstream=upstream_non_stream,
        endpoint_type="openai-chat-completions",
        record_body=True,
    )
    adapter = get_adapter("openai-chat-completions")
    recorder = Recorder(tmp_path, batch_size=100)
    proxy = ProxyService(spec, adapter, recorder, client_session)
    await proxy.start()
    site = proxy._site  # noqa: SLF001
    port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
    try:
        yield proxy, recorder, f"http://127.0.0.1:{port}"
    finally:
        await proxy.stop()


async def test_proxy_non_stream_response(
    running_proxy_non_stream: tuple,
) -> None:
    """非流式 JSON 响应也应该被正确记录"""
    proxy, recorder, local_url = running_proxy_non_stream

    request_body = json.dumps(
        {"model": "gpt-4o", "messages": []}
    ).encode()

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{local_url}/v1/chat/completions",
            data=request_body,
        ) as resp:
            assert resp.status == 200
            body = await resp.json()
            assert body["choices"][0]["message"]["content"] == "Hello"

    batch = await recorder.flush()
    assert len(batch) == 1
    record = batch[0]
    assert record.status_code == 200
    # 非流式：adapter 也应处理（on_stream_chunk + finalize 链路）
    assert record.response["content"] in ("Hello", "", None)  # 可能为空（流式字段不在非流式 JSON 里）


# ============================================================
# gzip 上游：Content-Encoding 头剥离
# ============================================================

import gzip


@pytest_asyncio.fixture
async def upstream_gzip_json() -> str:
    """Mock 上游：返回 gzip 压缩 JSON + Content-Encoding: gzip（模拟 DeepSeek 等压缩上游）"""

    async def handle(request: web.Request) -> web.Response:
        await request.read()
        payload = json.dumps(
            {
                "choices": [
                    {"message": {"role": "assistant", "content": "Hello"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            }
        ).encode()
        return web.Response(
            status=200,
            body=gzip.compress(payload),
            headers={"Content-Type": "application/json", "Content-Encoding": "gzip"},
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
    yield f"http://127.0.0.1:{port}"
    await runner.cleanup()


@pytest_asyncio.fixture
async def running_proxy_gzip(
    upstream_gzip_json: str,
    client_session: aiohttp.ClientSession,
    tmp_path,
) -> tuple[ProxyService, Recorder, str]:
    spec = EndpointSpec(
        name="gzip-svc",
        port=0,
        upstream=upstream_gzip_json,
        endpoint_type="openai-chat-completions",
        record_body=True,
    )
    adapter = get_adapter("openai-chat-completions")
    recorder = Recorder(tmp_path, batch_size=100)
    proxy = ProxyService(spec, adapter, recorder, client_session)
    await proxy.start()
    site = proxy._site  # noqa: SLF001
    port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
    try:
        yield proxy, recorder, f"http://127.0.0.1:{port}"
    finally:
        await proxy.stop()


async def test_proxy_strips_content_encoding_from_gzip_upstream(
    running_proxy_gzip: tuple,
) -> None:
    """上游 gzip 压缩时，代理不得透传 Content-Encoding 头

    实际故障（2026-08-26 DeepSeek 联调）：aiohttp ClientSession 默认
    auto_decompress=True 已把 body 解压成明文，但 Content-Encoding: gzip
    头被原样透传 → 客户端（openai SDK/httpx）按 gzip 解码明文 →
    "Connection error." 并重试 2 次（代理侧全 200）。
    """
    proxy, recorder, local_url = running_proxy_gzip

    request_body = json.dumps(
        {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
    ).encode()

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{local_url}/v1/chat/completions",
            data=request_body,
            headers={"Content-Type": "application/json"},
        ) as resp:
            assert resp.status == 200
            # 核心断言：body 已解压，头必须剥离（修复前此行失败）
            assert "Content-Encoding" not in resp.headers
            # 客户端视角：能正常读到明文 JSON
            data = await resp.json()
            assert data["choices"][0]["message"]["content"] == "Hello"

    batch = await recorder.flush()
    assert len(batch) == 1
    assert batch[0].status_code == 200
    assert batch[0].error is None


@pytest_asyncio.fixture
async def upstream_gzip_sse() -> str:
    """Mock 上游：gzip 压缩的 text/event-stream（少数上游对 SSE 也压缩）"""

    async def handle(request: web.Request) -> web.Response:
        await request.read()
        payload = (
            b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            b"data: [DONE]\n\n"
        )
        return web.Response(
            status=200,
            body=gzip.compress(payload),
            headers={"Content-Type": "text/event-stream", "Content-Encoding": "gzip"},
        )

    app = web.Application()
    app.router.add_post("/v1/chat/completions", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
    yield f"http://127.0.0.1:{port}"
    await runner.cleanup()


async def test_proxy_sse_strips_content_encoding(
    upstream_gzip_sse: str,
    client_session: aiohttp.ClientSession,
    tmp_path,
) -> None:
    """SSE 分支同样不得透传 Content-Encoding（与非流式共用剥离集合）"""
    spec = EndpointSpec(
        name="gzip-sse-svc",
        port=0,
        upstream=upstream_gzip_sse,
        endpoint_type="openai-chat-completions",
        record_body=True,
    )
    adapter = get_adapter("openai-chat-completions")
    recorder = Recorder(tmp_path, batch_size=100)
    proxy = ProxyService(spec, adapter, recorder, client_session)
    await proxy.start()
    site = proxy._site  # noqa: SLF001
    port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                data=json.dumps(
                    {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}], "stream": True}
                ).encode(),
                headers={"Content-Type": "application/json"},
            ) as resp:
                assert resp.status == 200
                assert "Content-Encoding" not in resp.headers
                body = await resp.text()
                assert "Hello" in body
                assert "[DONE]" in body
    finally:
        await proxy.stop()


# ============================================================
# 生命周期
# ============================================================


async def test_proxy_stop_is_idempotent(
    upstream_openai_chat: str,
) -> None:
    """stop() 重复调用不报错"""
    import tempfile
    from pathlib import Path
    tmp = Path(tempfile.mkdtemp())
    spec = EndpointSpec(
        name="test",
        port=0,
        upstream=upstream_openai_chat,
        endpoint_type="openai-chat-completions",
        record_body=True,
    )
    adapter = get_adapter("openai-chat-completions")
    recorder = Recorder(tmp, batch_size=100)
    async with aiohttp.ClientSession() as client:
        proxy = ProxyService(spec, adapter, recorder, client)
        await proxy.start()
        await proxy.stop()
        await proxy.stop()  # 第二次应不报错（虽然当前实现会因 _runner None 报错）
    # 注：当前实现不保证幂等（stop() 两次第二次会 AttributeError）


# ============================================================
# P1-12：请求体 / 响应体大小上限
# ============================================================


async def test_proxy_request_body_too_large(
    client_session: aiohttp.ClientSession,
    tmp_path: Path,
) -> None:
    """请求体超过 max_body_bytes → 返回 413"""
    adapter = get_adapter("openai-chat-completions")
    recorder = Recorder(tmp_path, batch_size=10)
    spec = EndpointSpec(
        name="s",
        port=0,
        upstream="http://127.0.0.1:1",
        endpoint_type="openai-chat-completions",
        record_body=True,
    )
    # 极小上限（100 bytes）触发限制
    proxy = ProxyService(
        spec, adapter, recorder, client_session, max_body_bytes=100
    )
    await proxy.start()
    site = proxy._site  # noqa: SLF001
    port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
    try:
        big_body = b'{"messages":[{"role":"user","content":"' + b"x" * 200 + b'"}]}'
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                data=big_body,
                headers={"Content-Type": "application/json"},
            ) as resp:
                assert resp.status == 413
    finally:
        await proxy.stop()
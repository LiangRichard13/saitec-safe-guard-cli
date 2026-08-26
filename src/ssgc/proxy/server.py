"""ProxyService — Layer 4

反向代理核心：用 aiohttp.web 起本地 HTTP 服务器，转发请求到上游 URL；
边透传 SSE 边累积给 adapter；调用 adapter.finalize()；构造 Record 并
recorder.enqueue()。

⚠️ 已知限制：
- 请求体 / 非流式响应体有大小上限（`max_body_bytes`，默认 100MB）防 OOM
- 上游超时通过 aiohttp.ClientTimeout 控制
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

import aiohttp
from aiohttp import web

from ..adapters.base import Adapter
from ..core.models import EndpointSpec, Record
from ..core.utils import now_iso8601, redact_headers
from ..recorder.recorder import Recorder

logger = logging.getLogger(__name__)

DEFAULT_MAX_BODY_BYTES = 100 * 1024 * 1024  # 100MB

# 响应头剥离集合：这些头描述的是上游 body 的传输表示，而代理已改变 body
# （读入内存 / 重新分块 / ClientSession 默认 auto_decompress 已解压），
# 透传会导致客户端解码失败——实测 DeepSeek 上游 gzip 压缩时，透传
# Content-Encoding 会让 openai SDK 按 gzip 解码明文，报 Connection error.
_STRIP_RESPONSE_HEADERS = frozenset({
    "content-length",
    "transfer-encoding",
    "content-encoding",
})


class ProxyService:
    """单个反向代理服务实例（对应一个本地端口 + 一个上游）"""

    def __init__(
        self,
        spec: EndpointSpec,
        adapter: Adapter,
        recorder: Recorder,
        http_client: aiohttp.ClientSession,
        upstream_timeout_sec: float = 60.0,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        event_sink: Any | None = None,
    ) -> None:
        self._spec = spec
        self._adapter = adapter
        self._recorder = recorder
        self._http_client = http_client
        self._upstream_timeout = aiohttp.ClientTimeout(total=upstream_timeout_sec)
        self._max_body_bytes = max_body_bytes
        # 事件钩子（Runtime 注入，monitor 用）：None 时无事件
        self._event_sink = event_sink

        self._app = web.Application()
        self._app.router.add_route("*", "/{path:.*}", self._handle)

        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._actual_port: int = self._spec.port  # start() 后会更新为实际端口

    async def _read_limited(self, reader: aiohttp.StreamReader, limit: int) -> bytes:
        """P1-12：分块读取，总字节超限抛 ValueError（防 OOM）"""
        chunks: list[bytes] = []
        total = 0
        while chunk := await reader.read(64 * 1024):
            total += len(chunk)
            if total > limit:
                raise ValueError(f"body exceeds {limit} bytes")
            chunks.append(chunk)
        return b"".join(chunks)

    # ============================================================
    # 生命周期
    # ============================================================

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "127.0.0.1", self._spec.port)
        await self._site.start()
        # 提取实际端口（支持 spec.port=0 自动分配）
        self._actual_port = self._site._server.sockets[0].getsockname()[1]  # noqa: SLF001
        logger.info("proxy started: %s -> %s on port %d",
                    self._spec.name, self._spec.upstream, self._actual_port)

    async def stop(self) -> None:
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        logger.info("proxy stopped: %s", self._spec.name)

    def status(self) -> dict[str, Any]:
        return {
            "name": self._spec.name,
            "port": self._actual_port,
            "upstream": self._spec.upstream,
            "endpoint_type": self._adapter.endpoint_type,
            "running": self._site is not None,
        }

    # ============================================================
    # 请求处理
    # ============================================================

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        """catch-all handler：转发任意 path 到 upstream"""
        record_id = str(uuid.uuid4())
        start = time.time()
        error_msg: str | None = None
        status_code: int = 0

        # 1. 读取请求体（P1-12：分块 + 大小上限防 OOM）
        try:
            request_body = await self._read_limited(request.content, self._max_body_bytes)
        except ValueError as e:
            error_msg = f"request body too large: {e}"
            return web.Response(status=413, text=error_msg)
        except Exception as e:
            error_msg = f"read request failed: {e}"
            request_body = b""

        # 2. 解析请求（adapter）
        try:
            parsed_req = self._adapter.parse_request(request_body)
        except Exception:
            parsed_req = {}

        # 3. 转发到上游
        upstream_url = self._spec.upstream.rstrip("/") + request.path
        forward_headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in ("host", "content-length", "transfer-encoding")
        }

        response: web.StreamResponse
        try:
            upstream_resp = await self._http_client.request(
                request.method,
                upstream_url,
                headers=forward_headers,
                data=request_body if request_body else None,
                params=request.query,
                timeout=self._upstream_timeout,
                allow_redirects=False,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            error_msg = f"upstream error: {e}"
            response = web.Response(status=502, text=str(e))
            self._submit(
                record_id, parsed_req, {}, request.path, start,
                status_code=502, error=error_msg,
            )
            return response

        status_code = upstream_resp.status

        # 4. 判断是否流式响应
        content_type = upstream_resp.headers.get("content-type", "")
        is_sse = content_type.startswith("text/event-stream")

        if is_sse:
            response = web.StreamResponse(
                status=upstream_resp.status,
                headers={
                    k: v
                    for k, v in upstream_resp.headers.items()
                    if k.lower() not in _STRIP_RESPONSE_HEADERS
                },
            )
            await response.prepare(request)

            try:
                async for chunk in upstream_resp.content.iter_any():
                    # 1. 透传给客户端
                    await response.write(chunk)
                    # 2. 累积给 adapter
                    try:
                        self._adapter.on_stream_chunk(chunk)
                    except Exception:
                        # 鲁棒性契约：on_stream_chunk 不应抛，但兜底 catch
                        logger.exception("adapter on_stream_chunk failed")
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                error_msg = f"upstream stream interrupted: {e}"

            try:
                await response.write_eof()
            except Exception:
                pass
            await upstream_resp.release()

            finalized = self._finalize_safe()
            self._submit(
                record_id, parsed_req, finalized, request.path, start,
                status_code=status_code, error=error_msg,
            )
            return response

        # 非流式：读全部 body（P1-12：分块 + 上限防 OOM）
        try:
            body = await self._read_limited(upstream_resp.content, self._max_body_bytes)
        except ValueError as e:
            error_msg = f"upstream response too large: {e}"
            return web.Response(status=502, text=error_msg)
        except Exception as e:
            error_msg = f"upstream read failed: {e}"
            body = b""

        # 也喂给 adapter（adapter 应能处理非流式 JSON）
        try:
            self._adapter.on_stream_chunk(body)
        except Exception:
            pass

        finalized = self._finalize_safe()
        await upstream_resp.release()

        response = web.Response(
            status=upstream_resp.status,
            body=body,
            headers={
                k: v
                for k, v in upstream_resp.headers.items()
                if k.lower() not in _STRIP_RESPONSE_HEADERS
            },
        )
        self._submit(
            record_id, parsed_req, finalized, request.path, start,
            status_code=status_code, error=error_msg,
        )
        return response

    # ============================================================
    # 内部辅助
    # ============================================================

    def _finalize_safe(self) -> dict[str, Any]:
        try:
            return self._adapter.finalize()
        except Exception:
            logger.exception("adapter finalize failed")
            return {"content": "", "finish_reason": None, "usage": None, "raw": None}

    def _submit(
        self,
        record_id: str,
        parsed_req: dict[str, Any],
        finalized: dict[str, Any],
        path: str,
        start: float,
        status_code: int,
        error: str | None,
    ) -> None:
        elapsed_ms = int((time.time() - start) * 1000)
        record = Record(
            record_id=record_id,
            service=self._spec.name,
            endpoint_type=self._adapter.endpoint_type,
            upstream=self._spec.upstream,
            path=path,
            timestamp=now_iso8601(),
            elapsed_ms=elapsed_ms,
            status_code=status_code,
            error=error,
            request=parsed_req,
            response=finalized,
        )
        try:
            self._recorder.enqueue(record)
        except Exception:
            logger.exception("recorder.enqueue failed")
        # 流量事件（monitor 实时输出用）
        if self._event_sink is not None:
            try:
                self._event_sink("traffic", {
                    "service": self._spec.name,
                    "path": path,
                    "status_code": status_code,
                    "elapsed_ms": elapsed_ms,
                    "error": error,
                })
            except Exception:
                pass  # 监控输出异常不影响代理转发
"""ProxyService — Layer 4

反向代理核心：起 HTTP 服务器，转发 + 流式透传 + 调用 adapter 重组 + 提交到 recorder。

⚠️ 骨架阶段：接口已定义，内部逻辑待 Phase C 落地。
"""
from __future__ import annotations

from pathlib import Path

import aiohttp

from ..adapters.base import Adapter
from ..core.models import EndpointSpec
from ..recorder.recorder import Recorder


class ProxyService:
    """单个反向代理服务实例（对应一个本地端口 + 一个上游）"""

    def __init__(
        self,
        spec: EndpointSpec,
        adapter: Adapter,
        recorder: Recorder,
        http_client: aiohttp.ClientSession,
    ) -> None:
        self._spec = spec
        self._adapter = adapter
        self._recorder = recorder
        self._http_client = http_client

    async def start(self) -> None:
        """启动 aiohttp 监听 `spec.port`"""
        raise NotImplementedError("Phase C 实现")

    async def stop(self) -> None:
        """优雅关闭：拒绝新请求 + flush 内存队列 + 关闭 HTTP 服务器"""
        raise NotImplementedError("Phase C 实现")

    def status(self) -> dict:
        """运行时状态（给 `safe-guard status` 用）"""
        raise NotImplementedError("Phase C 实现")
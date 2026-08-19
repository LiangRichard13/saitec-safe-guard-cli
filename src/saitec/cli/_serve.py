"""后台服务入口 — 被 `safe-guard start` 以子进程方式调用

运行 Runtime（前台），接收 SIGTERM 优雅关闭。**不对外注册 CLI 命令**。
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

from ..runtime.runtime import Runtime


def _setup_logging(config_path: Path, log_level: str) -> None:
    logs_dir = config_path.parent / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = logging.FileHandler(logs_dir / "safe-guard.log", encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    root.handlers = [handler]


async def _main(config_path: Path) -> int:
    runtime = Runtime.build_from(config_path=config_path)
    _setup_logging(config_path, runtime.config.log_level)

    stop_event = asyncio.Event()

    def _on_signal() -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            # Windows 无 signal handler
            pass

    try:
        await runtime.start()
    except Exception as e:
        logging.getLogger(__name__).error("启动失败: %s", e)
        return 2

    # 等待停止信号
    await stop_event.wait()
    await runtime.stop()
    return 0


def run_foreground(config_path: str | Path) -> int:
    """子进程入口（foreground）"""
    return asyncio.run(_main(Path(config_path)))


if __name__ == "__main__":
    sys.exit(run_foreground(sys.argv[1] if len(sys.argv) > 1 else "."))
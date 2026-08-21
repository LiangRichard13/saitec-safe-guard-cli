"""后台服务入口 — 被 `safe-guard start` 以子进程方式调用

运行 Runtime（前台），接收 SIGTERM（Unix）或 stop.flag 文件（Windows 兼容）优雅关闭。
**不对外注册 CLI 命令**。
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

# _serve.py 被 `safe-guard start` 以独立脚本方式（`python _serve.py <config>`）
# 调用，此时相对导入（`from ..runtime...`）没有父包会 ImportError，
# 因此这里用绝对导入（前提：saitec 已 pip 安装，见 pyproject 的 [project.scripts]）。
from saitec.runtime.runtime import Runtime


STOP_FLAG_NAME = "safe-guard.stop.flag"


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


def _stop_flag_path(config_path: Path) -> Path:
    return config_path.parent / STOP_FLAG_NAME


async def _watch_stop_flag(stop_event: asyncio.Event, config_path: Path) -> None:
    """P1-11：Windows 上 signal handler 不可用，轮询 stop.flag 文件"""
    flag = _stop_flag_path(config_path)
    while not stop_event.is_set():
        await asyncio.sleep(0.5)
        if flag.exists():
            stop_event.set()
            return


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
            # Windows：无 add_signal_handler，由 _watch_stop_flag 接管
            pass

    try:
        await runtime.start()
    except Exception as e:
        logging.getLogger(__name__).error("启动失败: %s", e)
        return 2

    # Windows 兜底：轮询 stop.flag 文件（stop.py Windows 路径会写此文件）
    watch_task = asyncio.create_task(_watch_stop_flag(stop_event, config_path))

    await stop_event.wait()
    watch_task.cancel()
    try:
        await watch_task
    except asyncio.CancelledError:
        pass
    # 清理 stop flag（防止下次启动残留）
    _stop_flag_path(config_path).unlink(missing_ok=True)

    await runtime.stop()
    return 0


def run_foreground(config_path: str | Path) -> int:
    """子进程入口（foreground）"""
    return asyncio.run(_main(Path(config_path)))


if __name__ == "__main__":
    sys.exit(run_foreground(sys.argv[1] if len(sys.argv) > 1 else "."))
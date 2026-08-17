"""stop — 优雅停止服务（SIGTERM → SIGKILL 兜底）"""
from __future__ import annotations

from pathlib import Path

import typer


def stop_cmd(
    ctx: typer.Context,
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
    timeout: int = typer.Option(
        10,
        "--timeout",
        help="等待优雅关闭的超时（秒）",
    ),
) -> None:
    """通过 PID 文件发送 SIGTERM，等待超时后 SIGKILL 兜底"""
    raise NotImplementedError("Phase E 实现")
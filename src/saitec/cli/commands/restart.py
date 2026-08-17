"""restart — 优雅重启（stop + start）"""
from __future__ import annotations

from pathlib import Path

import typer

from .start import start_cmd
from .stop import stop_cmd


def restart_cmd(
    ctx: typer.Context,
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
    timeout: int = typer.Option(10, "--timeout", help="stop 等待超时（秒）"),
) -> None:
    """stop + start 组合"""
    stop_cmd(ctx, config_path=config_path, json_output=json_output, timeout=timeout)
    start_cmd(ctx, config_path=config_path, json_output=json_output)
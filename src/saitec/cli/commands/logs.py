"""logs — 查看日志"""
from __future__ import annotations

from pathlib import Path

import typer


def logs(
    ctx: typer.Context,
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
    tail: int = typer.Option(
        100,
        "--tail",
        "-n",
        help="显示最后 N 行（默认 100）",
    ),
    follow: bool = typer.Option(
        False,
        "--follow",
        "-f",
        help="持续跟踪日志（类似 tail -f）",
    ),
    service: str | None = typer.Option(None, "--service", "-s", help="按服务过滤"),
) -> None:
    """查看日志（支持 --tail / --follow / --service）"""
    raise NotImplementedError("Phase E 实现")
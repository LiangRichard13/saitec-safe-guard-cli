"""report — 查询 SQLite 检测结果"""
from __future__ import annotations

from pathlib import Path

import typer


def report(
    ctx: typer.Context,
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
    service: str | None = typer.Option(None, "--service", "-s", help="按服务名过滤"),
    since: str | None = typer.Option(
        None,
        "--since",
        help="起始时间（ISO8601 或相对时间如 '1h'）",
    ),
    limit: int = typer.Option(100, "--limit", "-n", help="返回结果数上限"),
) -> None:
    """按时间 / 服务 / 结论查询 SQLite 里的检测结果"""
    raise NotImplementedError("Phase E 实现")
"""tail — 实时跟踪事件流（类似 tail -f JSONL）"""
from __future__ import annotations

from pathlib import Path

import typer


def tail(
    ctx: typer.Context,
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
    service: str | None = typer.Option(None, "--service", "-s", help="按服务过滤"),
    level: str | None = typer.Option(
        None,
        "--level",
        help="最低日志级别（debug/info/warning/error）",
    ),
) -> None:
    """实时跟踪 JSONL 事件流（按 --service / --level 过滤）"""
    raise NotImplementedError("Phase E 实现")
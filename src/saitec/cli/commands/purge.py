"""purge — 清理过期 JSONL + SQLite"""
from __future__ import annotations

from pathlib import Path

import typer


def purge(
    ctx: typer.Context,
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
    retention_days: int = typer.Option(
        30,
        "--retention-days",
        "-d",
        help="保留天数（默认 30）",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="只显示将要删除的内容，不实际删除",
    ),
) -> None:
    """清理 `retention_days` 之前的 JSONL 文件与 SQLite 记录"""
    raise NotImplementedError("Phase E 实现")
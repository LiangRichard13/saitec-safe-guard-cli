"""doctor — 自检（端口 / API key / 磁盘 / SQLite / JSONL）"""
from __future__ import annotations

from pathlib import Path

import typer


def doctor(
    ctx: typer.Context,
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
    quick: bool = typer.Option(
        False,
        "--quick",
        help="跳过 API 探测（仅本地检查）",
    ),
) -> None:
    """自检：端口可绑 / API key 有效 / 磁盘空间 / SQLite 完整性 / JSONL 可写"""
    raise NotImplementedError("Phase E 实现")
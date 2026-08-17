"""status — 查询运行状态"""
from __future__ import annotations

from pathlib import Path

import typer


def status_cmd(
    ctx: typer.Context,
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """查看各端口 / 上游 / 缓存积压 / 上报状态"""
    raise NotImplementedError("Phase E 实现")
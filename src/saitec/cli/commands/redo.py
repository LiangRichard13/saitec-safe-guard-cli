"""redo — 手动重报某条记录（绕过游标）"""
from __future__ import annotations

from pathlib import Path

import typer


def do_redo(
    ctx: typer.Context,
    record_id: str = typer.Argument(..., help="要重报的记录 ID（UUID）"),
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """从 JSONL 读出指定 `record_id`，绕过游标重新上报"""
    raise NotImplementedError("Phase E 实现")
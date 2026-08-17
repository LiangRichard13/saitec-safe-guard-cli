"""validate — 校验 config.json（不启动服务）"""
from __future__ import annotations

from pathlib import Path

import typer


def validate_cmd(
    ctx: typer.Context,
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """校验 config.json（含三级覆盖），不启动服务"""
    raise NotImplementedError("Phase E 实现")
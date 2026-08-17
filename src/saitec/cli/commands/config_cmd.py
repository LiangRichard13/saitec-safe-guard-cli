"""config — 配置管理（get / set / unset / list）

详见 `docs/design/saitec-safe-guard-cli-design.md` §15。
"""
from __future__ import annotations

from pathlib import Path

import typer

app = typer.Typer(help="配置管理（get / set / unset / list）")


@app.command(name="get")
def get_cmd(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="字段路径，如 detector.url / services.<name>.port"),
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """查看单个字段（按点路径）"""
    raise NotImplementedError("Phase E 实现")


@app.command(name="set")
def set_cmd(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="字段路径"),
    value: str = typer.Argument(..., help="字段值"),
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """修改单个字段（自动快照 + 校验 + 不自动重启）"""
    raise NotImplementedError("Phase E 实现")


@app.command(name="unset")
def unset_cmd(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="字段路径"),
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """清除字段（回退到默认）"""
    raise NotImplementedError("Phase E 实现")


@app.command(name="list")
def list_cmd(
    ctx: typer.Context,
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """列出所有字段（含来源：config / env / cli / default）"""
    raise NotImplementedError("Phase E 实现")
"""init — 交互式 / 非交互式生成 config.json"""
from __future__ import annotations

from pathlib import Path

import typer


def init_cmd(
    ctx: typer.Context,
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        envvar="SAITEC_API_KEY",
        help="X-API-Key（建议从 stdin 注入，避免 shell history）",
    ),
    detector_url: str | None = typer.Option(
        None,
        "--detector-url",
        envvar="SAITEC_DETECTOR_URL",
        help="检测服务器地址",
    ),
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """生成 config.json（默认到 platformdirs 用户目录；可显式 --config 覆盖）"""
    raise NotImplementedError("Phase E 实现")
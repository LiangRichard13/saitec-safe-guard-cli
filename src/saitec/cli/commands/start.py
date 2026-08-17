"""start — 启动服务（异步，PID 文件）"""
from __future__ import annotations

from pathlib import Path

import typer


def start_cmd(
    ctx: typer.Context,
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
    report_interval: int | None = typer.Option(
        None,
        "--report-interval",
        envvar="SAITEC_REPORT_INTERVAL",
        help="覆盖 detector.report_interval_sec",
    ),
    batch_size: int | None = typer.Option(
        None,
        "--batch-size",
        envvar="SAITEC_BATCH_SIZE",
        help="覆盖 detector.batch_size",
    ),
) -> None:
    """读配置，起多个反向代理端口，开始记录 + 定时上报"""
    raise NotImplementedError("Phase E 实现")
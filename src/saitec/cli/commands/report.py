"""report — 查询 SQLite 检测结果"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import typer

from .._common import SHIELD, WARN, console, emit, get_config_path, EXIT_USER_ERROR
from ...store.store import Store


def _parse_since(raw: str | None) -> datetime:
    """解析 --since：ISO8601 或相对时间（如 1h / 30m / 7d）"""
    if raw is None:
        return datetime.now(timezone.utc) - timedelta(hours=1)
    raw = raw.strip()
    if raw.endswith("h"):
        return datetime.now(timezone.utc) - timedelta(hours=int(raw[:-1]))
    if raw.endswith("m"):
        return datetime.now(timezone.utc) - timedelta(minutes=int(raw[:-1]))
    if raw.endswith("d"):
        return datetime.now(timezone.utc) - timedelta(days=int(raw[:-1]))
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise typer.BadParameter(f"无法解析时间: {raw}（支持 ISO8601 或 1h/30m/7d）")


def _run(since: datetime, service: str | None, limit: int, db_path: Path) -> list[dict]:
    async def _q() -> list[dict]:
        store = Store(db_path)
        results = await store.query(since, service=service, limit=limit)
        return [
            {
                "record_id": r.record_id,
                "service": r.service,
                "timestamp": r.timestamp,
                "detection_status": r.detection_status,
                "risk_level": r.risk_level,
                "model": r.model,
                "elapsed_ms": r.elapsed_ms,
                "detected_at": r.detected_at,
                "detail": r.detection_detail,
            }
            for r in results
        ]

    return asyncio.run(_q())


def report(
    ctx: typer.Context,
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
    service: str | None = typer.Option(None, "--service", "-s", help="按服务名过滤"),
    since: str | None = typer.Option(None, "--since", help="起始时间（ISO8601 或 1h/30m/7d）"),
    limit: int = typer.Option(100, "--limit", "-n", help="返回结果数上限"),
) -> None:
    """按时间 / 服务 / 结论查询 SQLite 里的检测结果"""
    path = config_path.expanduser().resolve() if config_path else get_config_path(ctx)
    db_path = path.parent / "results.db"

    if not db_path.exists():
        emit(json_output=json_output, ok=False,
             error={"code": "NO_DB", "message": f"检测结果库不存在: {db_path}（尚无上报）"},
             exit_code=EXIT_USER_ERROR)
        return

    since_dt = _parse_since(since)
    try:
        rows = _run(since_dt, service, limit, db_path)
    except Exception as e:
        emit(json_output=json_output, ok=False,
             error={"code": "QUERY_ERROR", "message": str(e)})
        return

    if json_output:
        emit(json_output=True,
             data={"count": len(rows), "since": since_dt.isoformat(), "results": rows})
        return

    # 人类可读：彩色表格
    if not rows:
        console.print("[dim]暂无检测记录（--since 时间窗口内无数据）[/dim]")
        return

    from rich.table import Table

    _STATUS_STYLE = {
        "violation": "[red]violation[/red]",
        "suspicious": "[yellow]suspicious[/yellow]",
        "clean": "[green]clean[/green]",
        "error": "[dim]error[/dim]",
    }
    _RISK_STYLE = {
        "critical": "[red]critical[/red]",
        "high": "[red]high[/red]",
        "medium": "[yellow]medium[/yellow]",
        "low": "[green]low[/green]",
    }

    table = Table(
        title=f"{SHIELD} 检测结果（{len(rows)} 条 · 自 {since_dt.isoformat(timespec='seconds')} 起）",
        title_style="cyan bold",
    )
    # 6 核心列（窄终端 79 宽可完整显示）；时间/理由等完整字段见 --json
    table.add_column("结论", no_wrap=True)
    table.add_column("风险", no_wrap=True)
    table.add_column("服务", style="cyan", no_wrap=True)
    table.add_column("模型", no_wrap=True)
    table.add_column("耗时", justify="right", no_wrap=True)
    table.add_column("记录 ID", style="dim", no_wrap=True)
    flagged: list[tuple[str, str, str]] = []  # (record_id, status, reason)
    for r in rows:
        reason = ""
        detail = r.get("detail") or {}
        if isinstance(detail, dict):
            reason = str(detail.get("reason") or "")
        status = r["detection_status"]
        if status in ("violation", "suspicious", "error") and reason:
            flagged.append(((r["record_id"] or "")[:8], status, reason))
        table.add_row(
            _STATUS_STYLE.get(status, status),
            _RISK_STYLE.get(r["risk_level"] or "", r["risk_level"] or "-"),
            r["service"],
            r["model"] or "-",
            f"{r['elapsed_ms']}ms",
            (r["record_id"] or "")[:8],
        )
    console.print(table)
    if flagged:
        console.print()
        console.print(f"{WARN} 风险摘要:")
        for rid, status, reason in flagged:
            console.print(f"  [bold]{rid}[/bold] [{ 'red' if status == 'violation' else 'yellow' }]{status}[/] — {reason}")
    console.print(f"[dim]完整字段（时间 / detected_at / detail）用 --json 查看；重报: safe-guard redo <record_id>[/dim]")
"""purge — 清理过期 JSONL + 日志切割备份 + SQLite"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import typer

from .._common import emit, get_config_path


def purge(
    ctx: typer.Context,
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
    retention_days: int = typer.Option(30, "--retention-days", "-d", help="保留天数（默认 30）"),
    dry_run: bool = typer.Option(False, "--dry-run", help="只显示将要删除的内容，不实际删除"),
) -> None:
    """🧹 清理过期数据（JSONL / 日志备份 / SQLite）

    清理三类：超期 JSONL 文件、日志切割备份（`ssgc.log.YYYY-MM-DD`）、
    SQLite 超期 detection_results 行。活跃日志文件与未超期数据不动。

    默认保留 30 天，可调。首次清理建议先用 `--dry-run` 预览。

    \b
    Examples:
      ssgc purge --dry-run                   # 预览（不删）
      ssgc purge                             # 清理 30 天前
      ssgc purge --retention-days 7          # 只留 7 天
      ssgc purge --retention-days 90 --json  # 留 90 天 + 结构化输出

    \b
    Troubleshooting:
      • doctor 报 disk fail → 用 purge 释放空间
      • 误清重要数据 → 从 detector 服务器拉历史（如果服务端有）

    \b
    See also:
      `ssgc doctor` 看磁盘占用  `ssgc logs` 看日志切割文件名
    """
    path = config_path.expanduser().resolve() if config_path else get_config_path(ctx)
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    cutoff_str = cutoff.date().isoformat()

    records_dir = path.parent / "records"
    db_path = path.parent / "results.db"

    removed_files: list[str] = []
    removed_log_files: list[str] = []
    removed_rows = 0

    # 1. JSONL 按天分片：文件名 records-YYYY-MM-DD.jsonl
    if records_dir.exists():
        for f in sorted(records_dir.glob("records-*.jsonl")):
            # 从文件名提取日期
            try:
                date_part = f.stem.replace("records-", "")
                if date_part < cutoff_str:
                    removed_files.append(f.name)
                    if not dry_run:
                        f.unlink(missing_ok=True)
            except ValueError:
                continue

    # 2. 日志切割备份：文件名 ssgc.log.YYYY-MM-DD，按日期 < cutoff 清理。
    #    活跃的 ssgc.log（无日期后缀）不删——可能正被 _serve 子进程写入。
    logs_dir = path.parent / "logs"
    if logs_dir.exists():
        for f in sorted(logs_dir.glob("ssgc.log.*")):
            date_part = f.name.replace("ssgc.log.", "")
            if len(date_part) == 10 and date_part < cutoff_str:  # YYYY-MM-DD
                removed_log_files.append(f.name)
                if not dry_run:
                    f.unlink(missing_ok=True)

    # 3. SQLite：删除 timestamp < cutoff 的记录
    if db_path.exists() and not dry_run:
        try:
            with sqlite3.connect(db_path) as conn:
                cur = conn.execute(
                    "DELETE FROM detection_results WHERE timestamp < ?",
                    (cutoff.isoformat(),),
                )
                removed_rows = cur.rowcount
                conn.commit()
        except sqlite3.Error:
            removed_rows = 0

    emit(json_output=json_output,
         data={
             "dry_run": dry_run,
             "retention_days": retention_days,
             "cutoff": cutoff_str,
             "removed_jsonl_files": removed_files,
             "removed_log_files": removed_log_files,
             "removed_sqlite_rows": removed_rows,
         })
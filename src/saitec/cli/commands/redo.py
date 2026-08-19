"""redo — 手动重报某条记录（绕过游标）"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiohttp
import typer

from .._common import emit, get_config_path
from ...core.config import load_config_json
from ...core.models import Record
from ...reporter.reporter import Reporter
from ...store.store import Store


def _find_record(records_dir: Path, record_id: str) -> Record | None:
    if not records_dir.exists():
        return None
    for f in sorted(records_dir.glob("records-*.jsonl")):
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("record_id") == record_id:
                return Record(
                    record_id=d["record_id"],
                    service=d["service"],
                    endpoint_type=d["endpoint_type"],
                    upstream=d["upstream"],
                    path=d["path"],
                    timestamp=d["timestamp"],
                    elapsed_ms=d["elapsed_ms"],
                    status_code=d["status_code"],
                    error=d.get("error"),
                    request=d.get("request", {}),
                    response=d.get("response", {}),
                )
    return None


def _run(record: Record, cfg_path: Path) -> dict:
    async def _redo() -> dict:
        config = load_config_json(cfg_path)
        db_path = cfg_path.parent / "results.db"
        async with aiohttp.ClientSession() as session:
            reporter = Reporter(config.detector, session)
            store = Store(db_path)
            results = await reporter.report([record])
            await store.save_results(results)
        return {
            "record_id": record.record_id,
            "reported": True,
            "detection_status": results[0].detection_status if results else None,
            "risk_level": results[0].risk_level if results else None,
        }

    return asyncio.run(_redo())


def do_redo(
    ctx: typer.Context,
    record_id: str = typer.Argument(..., help="要重报的记录 ID（UUID）"),
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """从 JSONL 读出指定 `record_id`，绕过游标重新上报"""
    path = config_path.expanduser().resolve() if config_path else get_config_path(ctx)

    record = _find_record(path.parent / "records", record_id)
    if record is None:
        emit(json_output=json_output, ok=False,
             error={"code": "RECORD_NOT_FOUND",
                    "message": f"在 JSONL 中未找到记录: {record_id}"})
        return

    try:
        result = _run(record, path)
    except Exception as e:
        emit(json_output=json_output, ok=False,
             error={"code": "REDO_ERROR", "message": str(e)})
        return

    emit(json_output=json_output, data=result)
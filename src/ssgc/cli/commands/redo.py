"""redo — 手动重报某条记录（绕过游标）"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiohttp
import typer

from .._common import emit, get_config_path, EXIT_USER_ERROR
from ...core.config import load_config_json
from ...core.models import Record
from ...reporter.reporter import Reporter
from ...store.store import Store


def _find_record(records_dir: Path, record_id: str) -> tuple[Record | None, list[str]]:
    """按 record_id 查 JSONL（支持完整 UUID 或前缀匹配）。

    返回 (record, candidates)：
      - 唯一匹配：(Record, [])
      - 未找到：(None, [])
      - 多条前缀匹配歧义：(None, [rid1, rid2, ...])
    """
    if not records_dir.exists():
        return None, []
    candidates: list[dict] = []
    for f in sorted(records_dir.glob("records-*.jsonl")):
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            rid = d.get("record_id")
            if not rid:
                continue
            if rid == record_id or rid.startswith(record_id):
                candidates.append(d)
    if len(candidates) == 1:
        d = candidates[0]
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
        ), []
    if len(candidates) > 1:
        return None, [c["record_id"] for c in candidates]
    return None, []


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
    record_id: str = typer.Argument(..., help="要重报的记录 ID（UUID 完整或前缀，前缀匹配多条时报错）"),
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """🔁 重报某条记录（绕过游标直接上报）

    从 JSONL 读出指定 record_id 的完整 Record，绕过 report_cursor 直接
    上报到 detector。常用于：
    - 之前上报因 5xx 失败、detector 修复后补报
    - detector 升级新规则、对历史记录重新判定
    - `ssgc report` 看到某条 `detection_status=error` 想重试

    **支持 UUID 前缀**：`ssgc redo 69f1285a`（8 位）即可，不必粘完整 UUID。
    前缀匹配多条时返回 `RECORD_ID_AMBIGUOUS` 错误列出候选完整 ID。

    \b
    Examples:
      ssgc redo 69f1285a                # 前缀匹配
      ssgc redo 69f1285a-b91d-48b9-8d1c-df3a1d3277b1  # 完整 UUID
      ssgc redo 69f1285a --json         # Agent 解析

    \b
    Troubleshooting:
      • RECORD_NOT_FOUND → JSONL 无此 ID（可能 purge 清了，用 `--json` 找别的）
      • RECORD_ID_AMBIGUOUS → 前缀撞多条，复制错误信息里的完整 ID 或用更长前缀
      • 上游 detector 不可达 → 报 REDO_ERROR；下次 detector 恢复后再试

    \b
    See also:
      `ssgc report` 找 record_id    `ssgc export` 导出整批
    """
    path = config_path.expanduser().resolve() if config_path else get_config_path(ctx)

    record, candidates = _find_record(path.parent / "records", record_id)
    if record is None:
        if candidates:
            emit(json_output=json_output, ok=False,
                 error={"code": "RECORD_ID_AMBIGUOUS",
                        "message": f"前缀 '{record_id}' 匹配到 {len(candidates)} 条记录，请提供更长的前缀或完整 UUID:\n"
                                   + "\n".join(f"  - {rid}" for rid in candidates)},
                 exit_code=EXIT_USER_ERROR)
        else:
            emit(json_output=json_output, ok=False,
                 error={"code": "RECORD_NOT_FOUND",
                        "message": f"在 JSONL 中未找到记录: {record_id}（可尝试更短前缀或用 ssgc report --json 拿完整 ID）"},
                 exit_code=EXIT_USER_ERROR)
        return

    try:
        result = _run(record, path)
    except Exception as e:
        emit(json_output=json_output, ok=False,
             error={"code": "REDO_ERROR", "message": str(e)})
        return

    emit(json_output=json_output, data=result)
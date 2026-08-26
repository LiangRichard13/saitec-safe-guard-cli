"""tail — 实时跟踪事件流（类似 tail -f JSONL）"""
from __future__ import annotations

import time
from pathlib import Path

import typer

from .._common import emit, get_config_path, EXIT_USER_ERROR


def tail(
    ctx: typer.Context,
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
    service: str | None = typer.Option(None, "--service", "-s", help="按服务过滤"),
    level: str | None = typer.Option(None, "--level", help="最低日志级别（debug/info/warning/error）"),
) -> None:
    """实时跟踪 JSONL 事件流（按 --service / --level 过滤）"""
    path = config_path.expanduser().resolve() if config_path else get_config_path(ctx)
    records_dir = path.parent / "records"

    if not records_dir.exists():
        emit(json_output=json_output, ok=False,
             error={"code": "NO_RECORDS", "message": f"records 目录不存在: {records_dir}"},
             exit_code=EXIT_USER_ERROR)
        return

    # 找到当前活跃的 JSONL 文件（今天的）
    files = sorted(records_dir.glob("records-*.jsonl"))
    if not files:
        emit(json_output=json_output, ok=False,
             error={"code": "NO_RECORDS", "message": "无 records-*.jsonl 文件"},
             exit_code=EXIT_USER_ERROR)
        return
    active = files[-1]

    print(f"# tailing {active} (Ctrl+C 退出)", flush=True)

    def _matches(line: str) -> bool:
        if service is not None and f'"service": "{service}"' not in line:
            return False
        if level is not None and f'"level": "{level}"' not in line:
            return False
        return True

    try:
        with open(active, "r", encoding="utf-8", errors="replace") as f:
            # 跳到文件末尾（只 tail 新内容）
            f.seek(0, 2)
            while True:
                line = f.readline()
                if line:
                    if _matches(line.strip()):
                        print(line.rstrip("\n"), flush=True)
                else:
                    time.sleep(0.3)
    except KeyboardInterrupt:
        pass
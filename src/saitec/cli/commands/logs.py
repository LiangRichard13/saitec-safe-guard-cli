"""logs — 查看日志"""
from __future__ import annotations

import time
from pathlib import Path

import typer

from .._common import emit, get_config_path


def logs(
    ctx: typer.Context,
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
    tail: int = typer.Option(100, "--tail", "-n", help="显示最后 N 行（默认 100）"),
    follow: bool = typer.Option(False, "--follow", "-f", help="持续跟踪日志（类似 tail -f）"),
    service: str | None = typer.Option(None, "--service", "-s", help="按服务过滤"),
) -> None:
    """查看日志（支持 --tail / --follow / --service）"""
    path = config_path.expanduser().resolve() if config_path else get_config_path(ctx)
    log_file = path.parent / "logs" / "safe-guard.log"

    if not log_file.exists():
        emit(json_output=json_output, ok=False,
             error={"code": "NO_LOG", "message": f"日志不存在: {log_file}（服务可能未启动）"})
        return

    def _filter(line: str) -> bool:
        if service is not None and service not in line:
            return False
        return True

    if not follow:
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        filtered = [l for l in lines if _filter(l)]
        emit(json_output=json_output, data=filtered[-tail:])
        return

    # follow 模式（仅人类可读；Agent 场景用 `safe-guard tail` 替代）
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            # 跳到末尾前 tail 行
            lines = f.readlines()
            start = max(0, len(lines) - tail)
            for line in lines[start:]:
                if _filter(line):
                    print(line, end="")
            f.seek(0, 2)
            while True:
                line = f.readline()
                if line:
                    if _filter(line):
                        print(line, end="")
                else:
                    time.sleep(0.5)
    except KeyboardInterrupt:
        pass
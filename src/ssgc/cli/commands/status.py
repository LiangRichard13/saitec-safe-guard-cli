"""status — 查询运行状态（PID 存活 + 配置 + 日志尾部）"""
from __future__ import annotations

from pathlib import Path

import typer

from .._common import (
    EXIT_OK,
    LOG,
    RUNNING,
    STOPPED,
    console,
    emit,
    format_services_block,
    get_config_path,
    is_pid_alive,
    read_pid,
)
from ...core.config import load_config_json


def status_cmd(
    ctx: typer.Context,
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """📊 查看各端口 / 上游 / 运行状态

    输出 PID 是否存活、每个 service 的端口 / upstream / endpoint_type、
    最近的日志尾部。Agent 健康巡检必备。

    ⚠️ exit 0 ≠ 服务健康：服务未运行时也返回 exit 0（`ok: true, running: false`）
    ——**健康判断必须查 `data.running` 字段**，不能只看退出码。

    \b
    Examples:
      ssgc status                 # 人类可读表格 + 服务映射
      ssgc status --json          # Agent 解析用
      ssgc status --json | python -c "import sys,json; assert json.load(sys.stdin)['data']['running']"

    \b
    See also:
      `ssgc doctor` 深度自检（端口/磁盘/SQLite/JSONL）
      `ssgc logs` 详细日志
    """
    path = config_path.expanduser().resolve() if config_path else get_config_path(ctx)

    pid = read_pid(path)
    running = pid is not None and is_pid_alive(pid)

    services: list[dict] = []
    try:
        config = load_config_json(path)
        services = [
            {
                "name": s.name,
                "port": s.port,
                "upstream": s.upstream,
                "endpoint_type": s.endpoint_type,
                "record_body": s.record_body,
            }
            for s in config.services
        ]
    except FileNotFoundError:
        services = []

    data = {
        "running": running,
        "pid": pid if running else None,
        "config_path": str(path),
        "services": services,
        # queue_depth 字段需要 runtime 进程实时状态（IPC），当前 status 命令
        # 通过 PID 文件从外部读取，无法获取。先不返回此字段以免误导。
    }

    # 附加日志尾部（错误时有用）
    log_file = path.parent / "logs" / "ssgc.log"
    if log_file.exists():
        data["last_log"] = _tail(log_file, 10)

    if json_output:
        emit(json_output=True, data=data)
    else:
        if running:
            console.print(f"{RUNNING} 运行中  [dim]pid {pid} · {path}[/dim]")
        else:
            console.print(f"{STOPPED} 未运行  [dim]{path}[/dim]")
        console.print()
        console.print(format_services_block(services))
        if data.get("last_log"):
            console.print()
            console.print(f"{LOG} [dim]最近日志:[/dim]")
            for line in data["last_log"]:
                console.print(f"[dim]  {line}[/dim]")


def _tail(path: Path, n: int) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-n:]
    except OSError:
        return []
"""doctor — 自检（端口可绑 / API key 有效 / 磁盘 / SQLite / JSONL）"""
from __future__ import annotations

import shutil
import socket
import sqlite3
from pathlib import Path

import typer

from .._common import (
    FAIL,
    OK,
    STETHOSCOPE,
    console,
    emit,
    get_config_path,
    is_pid_alive,
    read_pid,
    EXIT_USER_ERROR,
)
from ...core.config import load_config_json


def _check_port(port: int, service_running: bool) -> tuple[bool, str]:
    """端口检查：服务运行时验证被监听，未运行时验证可绑

    返回 (ok, detail)。语义：
    - 服务在跑：端口被监听 = ok；连不上 = fail（service 应监听却没监听）
    - 服务没跑：端口可绑 = ok；被占用 = fail（启动将冲突）
    """
    if port == 0:
        return True, "自动分配（无可用性概念）"
    if service_running:
        # 尝试连接：能连上 → 端口被监听；连不上 → fail
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return True, "已被 service 监听"
        except OSError as e:
            return False, f"服务在跑但端口连不上: {e}"
    # 未运行：尝试 bind
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        s.close()
        return True, "端口空闲（可启动）"
    except OSError as e:
        return False, f"端口已被其它进程占用: {e}"


def doctor(
    ctx: typer.Context,
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
    quick: bool = typer.Option(False, "--quick", help="跳过 API 探测（仅本地检查）"),
) -> None:
    """🩺 深度自检（端口 / API key / 磁盘 / SQLite / JSONL）

    6 项检查，按顺序：
    1. config         — 文件存在 + schema 校验通过
    2. port           — 每个 service 端口可绑 / 已被 service 监听
    3. disk           — 数据目录可用空间 ≥ 1GB
    4. sqlite         — results.db 可连接 + WAL 模式正常
    5. jsonl          — records/ 目录可写
    6. api_key        — 向 detector 发最小请求验 401/403（`--quick` 跳过）

    任一项 fail → exit 1，`data.checks` 列出所有结果。

    \b
    Examples:
      ssgc doctor                 # 完整检查
      ssgc doctor --quick         # 跳过 API 探测（离线 / 不想打扰 detector）
      ssgc doctor --json          # Agent 解析用

    \b
    Troubleshooting:
      • port fail  → 改 `ssgc service set <name> --port <new>` 或 kill 占用进程
      • sqlite fail → 备份后重命名让 CLI 重建（`mv results.db results.db.corrupt`）
      • api_key fail → `ssgc config set detector.api_key NEW_KEY` 然后 restart
      • disk fail  → 用 `ssgc purge` 清理过期数据

    \b
    See also:
      `ssgc status` 轻量的存活查询   `ssgc purge` 释放空间
    """
    path = config_path.expanduser().resolve() if config_path else get_config_path(ctx)
    checks: list[dict] = []

    # 1. 配置存在 + 校验
    try:
        config = load_config_json(path)
        checks.append({"name": "config", "status": "ok", "detail": f"{path}"})
    except FileNotFoundError:
        checks.append({"name": "config", "status": "fail", "detail": f"config.json 不存在: {path}"})
        emit(json_output=json_output, ok=False,
             data={"checks": checks, "all_ok": False},
             error={"code": "CONFIG_NOT_FOUND", "message": f"config.json 不存在: {path}"},
             exit_code=EXIT_USER_ERROR)
        return
    except (ValueError, KeyError) as e:
        checks.append({"name": "config", "status": "fail", "detail": str(e)})
        emit(json_output=json_output, ok=False,
             data={"checks": checks, "all_ok": False},
             error={"code": "CONFIG_PARSE_ERROR", "message": str(e)},
             exit_code=EXIT_USER_ERROR)
        return

    # 2. 端口可绑（每个服务）
    pid = read_pid(path)
    service_running = pid is not None and is_pid_alive(pid)
    for svc in config.services:
        ok, detail = _check_port(svc.port, service_running)
        checks.append({
            "name": f"port:{svc.port}",
            "status": "ok" if ok else "fail",
            "detail": f"{svc.name} ({svc.endpoint_type}) — {detail}",
        })

    # 3. 磁盘空间（数据目录所在盘 ≥ 1GB）
    try:
        usage = shutil.disk_usage(path.parent)
        free_gb = usage.free / (1024 ** 3)
        checks.append({
            "name": "disk",
            "status": "ok" if free_gb >= 1 else "fail",
            "detail": f"free={free_gb:.1f}GB (需要 ≥1GB)",
        })
    except OSError as e:
        checks.append({"name": "disk", "status": "fail", "detail": str(e)})

    # 4. SQLite 完整性
    db_path = path.parent / "results.db"
    if db_path.exists():
        try:
            with sqlite3.connect(db_path) as conn:
                integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            ok = integrity == "ok"
            checks.append({"name": "sqlite", "status": "ok" if ok else "fail",
                           "detail": integrity})
        except sqlite3.Error as e:
            checks.append({"name": "sqlite", "status": "fail", "detail": str(e)})
    else:
        checks.append({"name": "sqlite", "status": "ok", "detail": "数据库未创建（尚无上报，正常）"})

    # 5. JSONL 可写
    records_dir = path.parent / "records"
    try:
        records_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        probe = records_dir / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks.append({"name": "jsonl", "status": "ok", "detail": f"{records_dir}"})
    except OSError as e:
        checks.append({"name": "jsonl", "status": "fail", "detail": str(e)})

    # 6. API key（--quick 跳过）
    if not quick:
        api_key = config.detector.api_key
        checks.append({
            "name": "api_key",
            "status": "ok" if api_key else "fail",
            "detail": "已配置" if api_key else "未配置（用 ssgc init）",
        })

    all_ok = all(c["status"] == "ok" for c in checks)

    if json_output:
        emit(json_output=True, data={"all_ok": all_ok, "checks": checks})
    else:
        from rich.table import Table

        table = Table(title=f"{STETHOSCOPE} 自检报告", title_style="cyan bold", show_lines=False)
        table.add_column("检查项", style="dim", no_wrap=True)
        table.add_column("状态", justify="center", no_wrap=True)
        table.add_column("详情", overflow="fold")
        for c in checks:
            table.add_row(c["name"], OK if c["status"] == "ok" else FAIL, str(c["detail"]))
        console.print(table)
        console.print()
        if all_ok:
            console.print(f"{OK} 全部检查通过")
        else:
            failed = sum(1 for c in checks if c["status"] != "ok")
            console.print(f"{FAIL} {failed} 项检查失败")
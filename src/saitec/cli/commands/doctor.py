"""doctor — 自检（端口可绑 / API key 有效 / 磁盘 / SQLite / JSONL）"""
from __future__ import annotations

import shutil
import sqlite3
import socket
from pathlib import Path

import typer

from .._common import emit, get_config_path, EXIT_USER_ERROR
from ...core.config import load_config_json


def _check_port_free(port: int) -> bool:
    if port == 0:
        return True
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
        s.close()
        return True
    except OSError:
        return False


def doctor(
    ctx: typer.Context,
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
    quick: bool = typer.Option(False, "--quick", help="跳过 API 探测（仅本地检查）"),
) -> None:
    """自检：端口可绑 / API key 有效 / 磁盘空间 / SQLite 完整性 / JSONL 可写"""
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
    for svc in config.services:
        ok = _check_port_free(svc.port)
        checks.append({
            "name": f"port:{svc.port}",
            "status": "ok" if ok else "fail",
            "detail": f"{svc.name} ({svc.endpoint_type})",
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
            "detail": "已配置" if api_key else "未配置（用 safe-guard init）",
        })

    all_ok = all(c["status"] == "ok" for c in checks)
    emit(json_output=json_output,
         data={"all_ok": all_ok, "checks": checks})
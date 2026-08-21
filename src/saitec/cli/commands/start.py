"""start — 启动服务（异步：fork 子进程 + PID 文件）"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import typer

from .._common import (
    EXIT_OK,
    EXIT_RUNTIME_ERROR,
    client_env_hint as _client_env_hint,
    emit,
    format_services_block,
    get_config_path,
    is_pid_alive,
    read_pid,
    write_pid,
)
from ...core.config import validate_config


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
    path = config_path.expanduser().resolve() if config_path else get_config_path(ctx)

    # 1. 校验配置
    try:
        from ...core.config import apply_env_overrides, apply_cli_overrides, load_config_json
        config = load_config_json(path)
        config = apply_env_overrides(config)
        if report_interval is not None or batch_size is not None:
            config = apply_cli_overrides(
                config,
                report_interval=report_interval,
                batch_size=batch_size,
            )
        errors = validate_config(config)
    except FileNotFoundError:
        emit(json_output=json_output, ok=False,
             error={"code": "CONFIG_NOT_FOUND", "message": f"config.json 不存在: {path}"},
             exit_code=EXIT_RUNTIME_ERROR)
        return
    except (ValueError, KeyError) as e:
        emit(json_output=json_output, ok=False,
             error={"code": "CONFIG_PARSE_ERROR", "message": str(e)},
             exit_code=EXIT_RUNTIME_ERROR)
        return
    if errors:
        emit(json_output=json_output, ok=False,
             error={"code": "CONFIG_VALIDATION_ERROR",
                    "message": "配置校验失败（不启动）", "errors": [
                        {"field": e.field, "message": e.message} for e in errors
                    ]},
             exit_code=EXIT_RUNTIME_ERROR)
        return

    # 2. 检查已有实例
    pid = read_pid(path)
    if pid is not None and is_pid_alive(pid):
        emit(json_output=json_output, ok=False,
             error={"code": "ALREADY_RUNNING",
                    "message": f"服务已在运行 (PID {pid})。如需重启用 `safe-guard restart`"},
             exit_code=EXIT_RUNTIME_ERROR)
        return

    # 3. 启动子进程（foreground serve）
    serve_script = Path(__file__).parent.parent / "_serve.py"
    cmd = [sys.executable, str(serve_script), str(path)]
    # P1-9：CLI 覆盖转 SAITEC_* env 让子进程生效（_serve.py 走 env 覆盖）
    env_overrides: dict[str, str] = {}
    if report_interval is not None:
        env_overrides["SAITEC_REPORT_INTERVAL"] = str(report_interval)
    if batch_size is not None:
        env_overrides["SAITEC_BATCH_SIZE"] = str(batch_size)
    child_env = os.environ.copy()
    child_env.update(env_overrides)
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(path.parent),
            env=child_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=(os.name != "nt"),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except OSError as e:
        emit(json_output=json_output, ok=False,
             error={"code": "START_FAILED", "message": f"启动失败: {e}"},
             exit_code=EXIT_RUNTIME_ERROR)
        return

    write_pid(path, proc.pid)

    services_list = [
        {
            "name": s.name,
            "port": s.port,
            "upstream": s.upstream,
            "endpoint_type": s.endpoint_type,
        }
        for s in config.services
    ]
    if json_output:
        emit(json_output=True,
             data={
                 "started": True,
                 "pid": proc.pid,
                 "config_path": str(path),
                 "services": services_list,
                 "client_hint": {
                     s["name"]: _client_env_hint(s["endpoint_type"], s["port"])
                     for s in services_list
                 },
                 "log_file": str(path.parent / "logs" / "safe-guard.log"),
                 "applied_overrides": env_overrides,
             })
    else:
        print(f"started: True")
        print(f"pid: {proc.pid}")
        print(f"config_path: {path}")
        print()
        print(format_services_block(services_list))
        print()
        print(f"log_file: {path.parent / 'logs' / 'safe-guard.log'}")
        if env_overrides:
            print(f"applied_overrides: {env_overrides}")
        print()
        print("把客户端 base_url 指到上面的本地地址即可开始监控。")
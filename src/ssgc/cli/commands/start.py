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
    OK,
    ROCKET,
    client_env_hint as _client_env_hint,
    console,
    emit,
    format_services_block,
    get_config_path,
    get_version,
    is_pid_alive,
    print_banner,
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
        envvar="SSGC_REPORT_INTERVAL",
        help="覆盖 detector.report_interval_sec",
    ),
    batch_size: int | None = typer.Option(
        None,
        "--batch-size",
        envvar="SSGC_BATCH_SIZE",
        help="覆盖 detector.batch_size",
    ),
) -> None:
    """🚀 启动后台代理服务（读配置起端口 + 记录 + 上报）

    读取 ~/.ssgc/config.json（或 SSGC_CONFIG 指定的路径）配置，为每个 service
    启动一个反向代理端口，开始记录请求/响应到 JSONL，并按周期上报到检测服务器，
    检测结论落本地 SQLite。

    启动后 PID 写入 {config_dir}/ssgc.pid。服务已在运行时拒绝重复启动
    （返回 ALREADY_RUNNING，用 `ssgc restart` 替代）。

    \b
    Examples:
      ssgc start                         # 默认配置
      ssgc start --report-interval 5     # 临时 5s 上报间隔（调试用）
      SSGC_REPORT_INTERVAL=5 ssgc start  # 等价 env 形式
      ssgc start --batch-size 100        # 临时批量 100 条/批
      ssgc start --config /path/cfg      # 用指定配置

    \b
    Troubleshooting:
      • ALREADY_RUNNING   → 用 `ssgc restart` 或 `ssgc stop` 后重试
      • 端口冲突          → `ssgc doctor` 查哪个端口被占
      • 启动无报错但没跑  → 看 {config_dir}/logs/ssgc.log 尾部

    \b
    See also:
      `ssgc status`   查看运行状态     `ssgc stop`    优雅停止
      `ssgc monitor`  前台实时看流量   `ssgc restart` 重启生效配置
    """
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
                    "message": f"服务已在运行 (PID {pid})。如需重启用 `ssgc restart`"},
             exit_code=EXIT_RUNTIME_ERROR)
        return

    # 3. 启动子进程（foreground serve）
    serve_script = Path(__file__).parent.parent / "_serve.py"
    cmd = [sys.executable, str(serve_script), str(path)]
    # CLI 覆盖转 SSGC_* env 让子进程生效（_serve.py 走 env 覆盖）
    env_overrides: dict[str, str] = {}
    if report_interval is not None:
        env_overrides["SSGC_REPORT_INTERVAL"] = str(report_interval)
    if batch_size is not None:
        env_overrides["SSGC_BATCH_SIZE"] = str(batch_size)
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
                 "log_file": str(path.parent / "logs" / "ssgc.log"),
                 "applied_overrides": env_overrides,
             })
    else:
        print_banner(f"{ROCKET} v{get_version()} · pid {proc.pid} · {len(services_list)} 个服务监控中")
        console.print(f"{OK} 已启动  [dim]pid {proc.pid} · {path}[/dim]")
        console.print()
        console.print(format_services_block(services_list))
        console.print()
        console.print(f"[dim]日志: {path.parent / 'logs' / 'ssgc.log'}[/dim]")
        if env_overrides:
            console.print(f"[dim]临时覆盖: {env_overrides}[/dim]")
        console.print()
        console.print("把客户端 base_url 指到上面的本地地址即可开始监控。")
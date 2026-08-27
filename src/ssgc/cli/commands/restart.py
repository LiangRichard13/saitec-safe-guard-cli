"""restart — 优雅重启（stop + start）"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import typer

from .._common import (
    EXIT_OK,
    EXIT_RUNTIME_ERROR,
    OK,
    console,
    emit,
    get_config_path,
    get_version,
    is_pid_alive,
    print_banner,
    read_pid,
    remove_pid,
    write_pid,
)
from ...core.config import load_config_json, validate_config


def _terminate(pid: int, timeout: float) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=timeout)
        return
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_pid_alive(pid):
            return
        time.sleep(0.2)
    os.kill(pid, signal.SIGKILL)


def restart_cmd(
    ctx: typer.Context,
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
    timeout: int = typer.Option(10, "--timeout", help="stop 等待超时（秒）"),
) -> None:
    """🔄 优雅重启（stop + start）

    改了 config 后想让新配置生效用此命令。先 stop 旧进程（含最后一次 flush
    + 上报），再 start 新进程。重启前会先校验配置，校验失败拒绝重启
    （不会把正常运行的服务换成坏的）。

    \b
    Examples:
      ssgc restart                # 默认 10s stop 超时
      ssgc restart --timeout 30   # 大流量场景给足时间

    \b
    See also:
      `ssgc config set` 改配置后 `ssgc restart` 生效
      `ssgc start` 首次启动（无需 restart）
    """
    path = config_path.expanduser().resolve() if config_path else get_config_path(ctx)

    # 1. 校验配置
    try:
        config = load_config_json(path)
        errors = validate_config(config)
    except FileNotFoundError:
        emit(json_output=json_output, ok=False,
             error={"code": "CONFIG_NOT_FOUND", "message": f"config.json 不存在: {path}"},
             exit_code=EXIT_RUNTIME_ERROR)
        return
    if errors:
        emit(json_output=json_output, ok=False,
             error={"code": "CONFIG_VALIDATION_ERROR",
                    "message": "配置校验失败（不重启）"},
             exit_code=EXIT_RUNTIME_ERROR)
        return

    # 2. stop（若运行中）
    old_pid = read_pid(path)
    if old_pid is not None and is_pid_alive(old_pid):
        _terminate(old_pid, timeout)
    remove_pid(path)

    # 3. start
    serve_script = Path(__file__).parent.parent / "_serve.py"
    cmd = [sys.executable, str(serve_script), str(path)]
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(path.parent),
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

    if json_output:
        emit(json_output=True,
             data={
                 "restarted": True,
                 "old_pid": old_pid,
                 "new_pid": proc.pid,
                 "config_path": str(path),
             })
    else:
        print_banner(f"🔄 v{get_version()} · 重启完成 · pid {proc.pid}")
        console.print(
            f"{OK} 已重启  [dim]旧 pid {old_pid if old_pid else '无'} → 新 pid {proc.pid} · {path}[/dim]"
        )
        console.print()
        console.print("[dim]新配置已生效（如有修改）[/dim]")
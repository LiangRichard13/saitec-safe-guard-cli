"""stop — 优雅停止服务（SIGTERM → SIGKILL 兜底）"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import typer

from .._common import (
    EXIT_OK,
    EXIT_RUNTIME_ERROR,
    emit,
    get_config_path,
    is_pid_alive,
    read_pid,
    remove_pid,
)


def _terminate(pid: int, timeout: float) -> bool:
    """发送终止信号，等待优雅退出；超时则强杀"""
    if os.name == "nt":
        # Windows：先发 CTRL_BREAK（不友好），直接 taskkill
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            pass
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    # 等待优雅退出
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_pid_alive(pid):
            return True
        time.sleep(0.2)
    # 超时 → SIGKILL
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return True


def stop_cmd(
    ctx: typer.Context,
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
    timeout: int = typer.Option(10, "--timeout", help="等待优雅关闭的超时（秒）"),
) -> None:
    """通过 PID 文件发送 SIGTERM，等待超时后 SIGKILL 兜底"""
    path = config_path.expanduser().resolve() if config_path else get_config_path(ctx)
    pid = read_pid(path)

    if pid is None:
        emit(json_output=json_output, ok=False,
             error={"code": "NOT_RUNNING", "message": "未找到 PID 文件（服务未运行）"},
             exit_code=EXIT_RUNTIME_ERROR)
        return

    if not is_pid_alive(pid):
        remove_pid(path)
        emit(json_output=json_output, ok=False,
             error={"code": "STALE_PID", "message": f"PID {pid} 已失效，已清理 PID 文件"},
             exit_code=EXIT_RUNTIME_ERROR)
        return

    _terminate(pid, timeout)
    remove_pid(path)
    emit(json_output=json_output,
         data={"stopped": True, "pid": pid, "timeout_sec": timeout})
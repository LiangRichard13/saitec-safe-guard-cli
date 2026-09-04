"""stop — 优雅停止服务（SIGTERM → SIGKILL 兜底，Windows 用 stop_flag 文件）"""
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
from .._serve import STOP_FLAG_NAME


def _stop_flag(config_path: Path) -> Path:
    return config_path.parent / STOP_FLAG_NAME


def _terminate(pid: int, timeout: float, config_path: Path) -> bool:
    """发送终止信号，等待优雅退出；超时则强杀

    Windows：先写 stop.flag 让 _serve.py 优雅关闭；超时再 taskkill /F。
    """
    if os.name == "nt":
        # 写 stop flag 触发 _serve 的 watch task 优雅关闭
        _stop_flag(config_path).touch()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not is_pid_alive(pid):
                return True
            time.sleep(0.2)
        # 兜底强杀
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            pass
        return True

    # Unix: SIGTERM → SIGKILL
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_pid_alive(pid):
            return True
        time.sleep(0.2)
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
    """🛑 优雅停止后台代理服务

    Unix：发送 SIGTERM，等待超时后 SIGKILL 兜底。
    Windows：写 `{config_dir}/ssgc.stop.flag`，`_serve.py` 子进程轮询检测后
    优雅退出；超时后 `taskkill /F` 强杀。

    停止前会触发最后一次 JSONL flush + 上报，确保内存里记录不丢。

    \b
    Examples:
      ssgc stop                # 默认 10s 超时
      ssgc stop --timeout 30   # 大流量场景给足时间
      ssgc stop --json         # 返回 {"ok": true, "data": {"stopped": true, "pid": ...}}

    \b
    Troubleshooting:
      • NOT_RUNNING → PID 文件不存在，服务本就没跑
      • STALE_PID → PID 指向的进程已死但文件残留，stop 会自动清理
      • 超时仍停不掉 → 手动 `taskkill /F /PID <pid>` 或删 `ssgc.pid` 文件
    """
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

    _terminate(pid, timeout, path)
    remove_pid(path)
    _stop_flag(path).unlink(missing_ok=True)
    emit(json_output=json_output,
         data={"stopped": True, "pid": pid, "timeout_sec": timeout})
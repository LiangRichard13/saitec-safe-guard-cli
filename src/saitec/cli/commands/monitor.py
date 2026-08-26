"""monitor — 前台实时监控（人盯场景）

一个进程既是 runtime（起代理 + 上报）又是实时输出面板：
正常流量 dim 单行简报，异常（violation / 上报失败 / AUTH 停摆）彩色醒目。
Ctrl+C 或 `safe-guard stop`（stop.flag）优雅退出并停服务。

与 Agent 心跳定时任务互补：monitor 给人看（实时值守），心跳给 Agent 看（异步汇报）。
无 --json——此命令本质是给人看的实时流。
"""
from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path
from typing import Any

import typer

from .._common import (
    EXIT_RUNTIME_ERROR,
    FAIL,
    RADAR,
    WARN,
    console,
    emit,
    format_services_block,
    get_config_path,
    is_pid_alive,
    print_banner,
    read_pid,
    remove_pid,
    write_pid,
)
from .._serve import STOP_FLAG_NAME
from ...core.config import load_config_json, validate_config
from ...runtime.runtime import Runtime


def _stop_flag_path(config_path: Path) -> Path:
    return config_path.parent / STOP_FLAG_NAME


async def _watch_stop_flag(stop_event: asyncio.Event, config_path: Path) -> None:
    """Windows 上 Ctrl+C 不可靠时，`safe-guard stop` 写 stop.flag 兜底"""
    flag = _stop_flag_path(config_path)
    while not stop_event.is_set():
        await asyncio.sleep(0.5)
        if flag.exists():
            stop_event.set()
            return


async def _monitor_main(config_path: Path, report_interval: int | None,
                        batch_size: int | None) -> int:
    stats = {"traffic": 0, "flagged": 0, "report_errors": 0}
    started_services: list[dict[str, Any]] = []

    def on_event(kind: str, payload: dict[str, Any]) -> None:
        ts = time.strftime("%H:%M:%S")
        if kind == "started":
            started_services.extend(payload.get("services", []))
            return
        if kind == "traffic":
            stats["traffic"] += 1
            if payload.get("error") or payload.get("status_code", 0) >= 400:
                console.print(
                    f"[dim]{ts}[/dim] {FAIL} {payload['service']} {payload['path']} "
                    f"[red]{payload['status_code']}[/red] "
                    f"[red]{(payload.get('error') or '')[:80]}[/red]"
                )
            else:
                console.print(
                    f"[dim]{ts} → {payload['service']} {payload['path']} "
                    f"{payload['status_code']} {payload['elapsed_ms']}ms[/dim]"
                )
        elif kind == "report":
            flagged = payload.get("flagged", [])
            stats["flagged"] += len(flagged)
            if not flagged:
                console.print(f"[dim]{ts} ↑ 上报 {payload.get('total', 0)} 条（无异常）[/dim]")
                return
            console.print(f"[dim]{ts}[/dim] ⚠ [bold]上报 {payload.get('total', 0)} 条，"
                          f"[red]{len(flagged)} 条需关注[/red][/bold]")
            for f in flagged:
                status = f.get("detection_status", "?")
                style = "red" if status == "violation" else "yellow"
                console.print(
                    f"     [{style}]{status:<10}[/] "
                    f"[bold]{(f.get('record_id') or '')[:8]}[/bold] "
                    f"{f.get('risk_level') or '-'}  {f.get('reason', '')}"
                )
        elif kind == "report_error":
            stats["report_errors"] += 1
            console.print(f"[dim]{ts}[/dim] {WARN} 上报失败({payload.get('kind')}) "
                          f"[yellow]{(payload.get('message') or '')[:100]}[/yellow]")
        elif kind == "auth_failed":
            stats["report_errors"] += 1
            console.print(f"[dim]{ts}[/dim] {FAIL} [bold red]X-API-Key 失效，上报已停摆[/bold red]")
            console.print(f"     [yellow]{(payload.get('message') or '')[:120]}[/yellow]")
        elif kind == "stopped":
            return  # 总结在外层打

    overrides: dict[str, Any] = {}
    if report_interval is not None:
        overrides["report_interval"] = report_interval
    if batch_size is not None:
        overrides["batch_size"] = batch_size
    runtime = Runtime.build_from(config_path=config_path, event_sink=on_event, **overrides)

    stop_event = asyncio.Event()

    def _on_signal() -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            pass  # Windows：Ctrl+C 走 KeyboardInterrupt，stop.flag 兜底

    write_pid(config_path, os.getpid())

    try:
        await runtime.start()
    except Exception as e:
        remove_pid(config_path)
        console.print(f"{FAIL} 启动失败: {e}")
        return 2

    print_banner("🛰️ monitor 模式 · Ctrl+C 退出（safe-guard stop 亦可）")
    console.print(format_services_block(started_services))
    console.print(f"[dim]检测服务器: {runtime.config.detector.url} · "
                  f"上报周期 {runtime.config.detector.report_interval_sec}s[/dim]")
    console.print()

    watch_task = asyncio.create_task(_watch_stop_flag(stop_event, config_path))
    try:
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    watch_task.cancel()
    try:
        await watch_task
    except asyncio.CancelledError:
        pass
    _stop_flag_path(config_path).unlink(missing_ok=True)

    await runtime.stop()
    remove_pid(config_path)
    console.print()
    console.print(f"{RADAR} 会话结束: [bold]{stats['traffic']}[/bold] 条流量 · "
                  f"[red]{stats['flagged']}[/red] 条需关注 · "
                  f"[yellow]{stats['report_errors']}[/yellow] 次上报失败")
    return 0


def monitor_cmd(
    ctx: typer.Context,
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    report_interval: int | None = typer.Option(
        None, "--report-interval", envvar="SAITEC_REPORT_INTERVAL",
        help="临时覆盖上报周期秒数（monitor 场景常调小，如 5）",
    ),
    batch_size: int | None = typer.Option(
        None, "--batch-size", envvar="SAITEC_BATCH_SIZE", help="临时覆盖批量大小",
    ),
) -> None:
    """前台实时监控：起服务 + 终端实时输出异常（Ctrl+C 退出）"""
    path = config_path.expanduser().resolve() if config_path else get_config_path(ctx)

    try:
        config = load_config_json(path)
        errors = validate_config(config)
    except FileNotFoundError:
        emit(json_output=False, ok=False,
             error={"code": "CONFIG_NOT_FOUND", "message": f"config.json 不存在: {path}"},
             exit_code=EXIT_RUNTIME_ERROR)
        return
    except ValueError as e:
        emit(json_output=False, ok=False,
             error={"code": "CONFIG_PARSE_ERROR", "message": str(e)},
             exit_code=EXIT_RUNTIME_ERROR)
        return
    if errors:
        emit(json_output=False, ok=False,
             error={"code": "CONFIG_VALIDATION_ERROR", "message": "配置校验失败（不启动）"},
             exit_code=EXIT_RUNTIME_ERROR)
        return

    # 互斥：与 start/restart 共用 PID 检查
    pid = read_pid(path)
    if pid is not None and is_pid_alive(pid):
        emit(json_output=False, ok=False,
             error={"code": "ALREADY_RUNNING",
                    "message": f"服务已在运行 (PID {pid})。monitor 需独占前台，请先 stop"},
             exit_code=EXIT_RUNTIME_ERROR)
        return

    try:
        code = asyncio.run(_monitor_main(path, report_interval, batch_size))
    except KeyboardInterrupt:
        # Windows 下 Ctrl+C 可能直接炸出 asyncio.run
        remove_pid(path)
        _stop_flag_path(path).unlink(missing_ok=True)
        console.print("\n[dim]已中断[/dim]")
        code = 0
    raise typer.Exit(code)

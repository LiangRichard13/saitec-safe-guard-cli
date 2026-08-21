"""CLI 入口 — Layer 6

详见 `docs/design/saitec-safe-guard-cli-design.md` §13（13 个命令）。

⚠️ 骨架阶段：13 个命令占位，业务实现在 Phase E 落地。
"""
from __future__ import annotations

import sys
from pathlib import Path

import typer

from .commands import config_cmd, doctor, init, logs, purge, redo, report, restart, service_cmd, start, status, stop, tail, validate


def _configure_io_encoding() -> None:
    """Windows 下保证 UTF-8 输出，避免 GBK 代码页把中文/JSON 编码掉。

    策略：
    - stdout/stderr 是 pipe（重定向、Agent 调用、pytest 收集）→ 强制 UTF-8
      （Agent 解析 JSON 必须 UTF-8，否则中文/非 ASCII 字段乱码）
    - stdout/stderr 是 tty（用户在 cmd.exe / PowerShell 交互）→ 跟随系统编码
      （保留 Windows 原生 cmd.exe 的 GBK 渲染，避免在老终端里强制 UTF-8 反而乱码）
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            # reconfigure 在某些环境不可用（如重定向到不支持的对象）；忽略
            pass


_configure_io_encoding()


app = typer.Typer(
    name="safe-guard",
    help="监控大模型 API 调用的反向代理 CLI",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main(
    ctx: typer.Context,
    config_path: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        envvar="SAITEC_CONFIG",
        help="配置文件路径（默认由 platformdirs 解析）",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="输出机器可读 JSON（Agent 友好）",
    ),
) -> None:
    """所有命令全局支持 --config 与 --json"""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path
    ctx.obj["json_output"] = json_output


# 注册顶层命令
app.command(name="init")(init.init_cmd)
app.command(name="start")(start.start_cmd)
app.command(name="stop")(stop.stop_cmd)
app.command(name="restart")(restart.restart_cmd)
app.command(name="status")(status.status_cmd)
app.command(name="report")(report.report)
app.command(name="validate")(validate.validate_cmd)
app.command(name="doctor")(doctor.doctor)
app.command(name="logs")(logs.logs)
app.command(name="tail")(tail.tail)
app.command(name="redo")(redo.do_redo)
app.command(name="purge")(purge.purge)

# config 子命令组（get / set / unset / list）
app.add_typer(config_cmd.app, name="config")

# service 子命令组（add / remove / set / list）
app.add_typer(service_cmd.app, name="service")


if __name__ == "__main__":
    app()
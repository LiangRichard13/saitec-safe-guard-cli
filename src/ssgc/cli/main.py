"""CLI 入口 — Layer 6

详见 `docs/design/saitec-safe-guard-cli-design.md` §13。
"""
from __future__ import annotations

import sys
from pathlib import Path

import typer

from .commands import config_cmd, doctor, export, init, logs, monitor, purge, redo, report, restart, service_cmd, start, status, stop, tail, validate


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


# help 正文样式：typer 默认灰色（dim），改为白色便于终端阅读
try:
    import typer.rich_utils as _rich_utils  # type: ignore[import-untyped]
    _rich_utils.STYLE_HELPTEXT = "white"
    _rich_utils.STYLE_HELPTEXT_FIRST_LINE = "white bold"
except (ImportError, AttributeError):
    pass  # typer 老版本不暴露这些常量，忽略


app = typer.Typer(
    name="ssgc",
    help="""🛡️  ssgc — 大模型 API 流量反向代理监控

把所有大模型 API 请求转一道到本地端口，透明转发到真实上游，
同时记录请求/响应到 JSONL，周期上报到内部安全检测服务器，
检测结论落本地 SQLite 供查询。

\b
🚀 Quick start:
  ssgc init --api-key KEY --detector-url URL --upstream URL
  ssgc start
  ssgc status
  ssgc report
""",
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
        envvar="SSGC_CONFIG",
        help="配置文件路径（默认 ~/.ssgc/config.json）",
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
app.command(name="monitor")(monitor.monitor_cmd)
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
app.command(name="export")(export.do_export)

# config 子命令组（get / set / unset / list）
app.add_typer(config_cmd.app, name="config")

# service 子命令组（add / remove / set / list）
app.add_typer(service_cmd.app, name="service")


if __name__ == "__main__":
    app()
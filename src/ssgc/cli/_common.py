"""CLI 共享辅助

- 配置路径解析（--config / SSGC_CONFIG / ~/.ssgc 默认）
- 输出契约（人类可读 / JSON，见 architecture.md §4 Layer 6）
- 人类可读输出的统一视觉（rich：颜色 / 图标 / banner）
- PID 文件管理（跨平台）
"""
from __future__ import annotations

import json
import os
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from ..core.models import AppConfig, ConfigError
from ..core.paths import resolve_config_dir, resolve_config_path

EXIT_OK = 0
EXIT_USER_ERROR = 1
EXIT_RUNTIME_ERROR = 2
EXIT_DETECTOR_ERROR = 3
EXIT_INTERNAL_ERROR = 4

# ============================================================
# 视觉系统（rich）
# ============================================================

# stdout（数据）/ stderr（错误、警告）分离，与既有输出契约一致
console = Console()
err_console = Console(stderr=True)

# 图标集（emoji 为主：自带色彩、跨 Windows Terminal / Git Bash / PowerShell 渲染；
# 非 TTY 时 rich 自动去 markup 色，emoji 字符保留）
OK = "✅"          # 成功
FAIL = "❌"        # 失败 / 错误
WARN = "⚠️ "       # 警告
INFO = "💡"        # 提示
RUNNING = "🟢"     # 运行中
STOPPED = "⚪"     # 未运行
ROCKET = "🚀"      # 启动
SHIELD = "🛡️"      # 检测 / 安全
RADAR = "📡"       # 服务映射 / 监控
STETHOSCOPE = "🩺" # 诊断 / doctor
GEAR = "⚙️"        # 配置
BROOM = "🧹"       # 清理
LOG = "📜"         # 日志
CHART = "📊"       # 报表 / 报告

# 品牌 banner（figlet 风格，仅 start/restart 成功后展示）
_BANNER_ART = r"""
 ____    ____    ____    ____
/ ___|  / ___|  / ___|  / ___|
\___ \  | |  _  | |      \___ \
 ___) | | |_| | | |___   ___) |
|____/   \____|  \____| |____/
""".strip("\n")


def get_version() -> str:
    """从已安装的包元数据读版本（pyproject.toml 是唯一版本来源，避免硬编码副本漂移）"""
    try:
        return _pkg_version("saitec-safe-guard-cli")
    except PackageNotFoundError:
        return "0.0.0-dev"


def print_banner(subtitle: str = "") -> None:
    """品牌 banner：青色大字 + dim 副标题（如版本与 pid）"""
    console.print(_BANNER_ART, style="cyan")
    if subtitle:
        console.print(subtitle, style="dim")
    console.print()


# ============================================================
# 输出
# ============================================================


def emit(
    *,
    json_output: bool,
    ok: bool = True,
    data: Any = None,
    error: dict[str, Any] | None = None,
    exit_code: int = EXIT_OK,
) -> None:
    """统一输出：JSON 形态（Agent 友好）或人类可读形态

    数据 → stdout；错误/日志 → stderr。
    """
    if json_output:
        payload: dict[str, Any] = {"ok": ok}
        if data is not None:
            payload["data"] = data
        if error is not None:
            payload["error"] = error
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        if error is not None:
            err_console.print(f"{FAIL} 错误: {error.get('message', '')}")
        elif data is not None:
            _render_human(data)

    if exit_code != EXIT_OK:
        raise typer.Exit(exit_code)


def _render_human(data: Any) -> None:
    """人类可读输出：key 用 dim 标签、value 原色；list 逐项"""
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, (dict, list)):
                console.print(f"[dim]{k}:[/dim] {json.dumps(v, ensure_ascii=False)}")
            else:
                console.print(f"[dim]{k}:[/dim] {v}")
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                console.print(json.dumps(item, ensure_ascii=False))
            else:
                console.print(item)
    elif isinstance(data, str):
        # 字符串原样打印（可含 rich markup，如 format_services_block 的成品块）
        console.print(data)
    else:
        console.print(json.dumps(data, ensure_ascii=False))


# ============================================================
# 配置路径
# ============================================================


def get_config_path(ctx: typer.Context) -> Path:
    """从 ctx.obj 取配置路径（或环境变量 SSGC_CONFIG / ~/.ssgc 默认）

    注意：sub-typer 命令（config get/set/...）的 ctx.obj 不继承主 callback 的修改，
    所以这里显式回退到环境变量。
    """
    try:
        explicit = (ctx.obj or {}).get("config_path")
        if explicit is not None:
            return Path(explicit).expanduser().resolve()
    except (AttributeError, TypeError):
        pass
    env_var = os.environ.get("SSGC_CONFIG")
    if env_var:
        return Path(env_var).expanduser().resolve()
    return resolve_config_path()


def resolve_data_dir_for(config_path: Path) -> Path:
    """数据目录跟随 config 目录"""
    return config_path.parent


# ============================================================
# 错误格式化
# ============================================================


def format_errors(errors: list[ConfigError]) -> list[dict[str, str]]:
    return [{"code": e.code.value, "field": e.field, "message": e.message} for e in errors]


# ============================================================
# PID 文件
# ============================================================


def pid_file_path(config_path: Path) -> Path:
    return config_path.parent / "ssgc.pid"


def write_pid(config_path: Path, pid: int) -> None:
    pid_file_path(config_path).write_text(str(pid), encoding="utf-8")


def read_pid(config_path: Path) -> int | None:
    p = pid_file_path(config_path)
    if not p.exists():
        return None
    try:
        return int(p.read_text().strip())
    except (ValueError, OSError):
        return None


def remove_pid(config_path: Path) -> None:
    try:
        pid_file_path(config_path).unlink(missing_ok=True)
    except OSError:
        pass


def is_pid_alive(pid: int) -> bool:
    """跨平台检查进程是否存活"""
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# ============================================================
# 日志路径
# ============================================================


def log_file_path(config_path: Path) -> Path:
    return config_path.parent / "logs" / "ssgc.log"


def records_dir_for(config_path: Path) -> Path:
    return config_path.parent / "records"


def db_path_for(config_path: Path) -> Path:
    return config_path.parent / "results.db"


# ============================================================
# 服务映射展示
# ============================================================


def client_base_url(endpoint_type: str, port: int) -> str:
    """按 endpoint_type 给出客户端应配置的本地 base_url

    - openai 系：SDK 习惯 base_url 以 /v1 结尾（请求时拼 /chat/completions）
    - anthropic 系：SDK 只要 host（请求时自拼 /v1/messages）
    """
    if endpoint_type == "anthropic-messages":
        return f"http://127.0.0.1:{port}"
    return f"http://127.0.0.1:{port}/v1"


def client_env_hint(endpoint_type: str, port: int) -> str:
    """按 endpoint_type 给出客户端环境变量配置提示"""
    if endpoint_type == "anthropic-messages":
        return f"ANTHROPIC_BASE_URL={client_base_url(endpoint_type, port)}"
    return f"OPENAI_BASE_URL={client_base_url(endpoint_type, port)}"


def format_services_block(services: list[dict]) -> str:
    """人类可读的服务映射块：name / 客户端地址 / 本地端口 / 真实上游

    services 元素字段：name / port / upstream / endpoint_type。
    返回含 rich markup 的字符串（emit 的 str 分支按 markup 渲染）。
    关键串（客户端配置 URL）保持一整行完整。
    """
    if not services:
        return f"{INFO} 无监控服务，用 [cyan]ssgc service add <name> --upstream <URL>[/cyan] 添加"
    lines = [f"{RADAR} [cyan bold]服务映射[/cyan bold] [dim]（客户端 base_url → 本地端口 → 真实上游）[/dim]"]
    for i, s in enumerate(services, 1):
        port = s.get("port", 0)
        port_str = str(port) if port else "自动分配"
        lines.append(
            f"  {i}. [cyan]{s.get('name', '?')}[/cyan] [dim]\\[{s.get('endpoint_type', '?')}][/dim]"
            f"  127.0.0.1:{port_str}  [dim]→[/dim]  {s.get('upstream', '?')}"
        )
        lines.append(f"     [dim]客户端配置:[/dim] {client_env_hint(s.get('endpoint_type', ''), port)}")
    return "\n".join(lines)
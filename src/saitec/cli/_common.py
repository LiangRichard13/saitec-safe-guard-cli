"""CLI 共享辅助

- 配置路径解析（--config / SAITEC_CONFIG / platformdirs）
- 输出契约（人类可读 / JSON，见 architecture.md §4 Layer 6）
- PID 文件管理（跨平台）
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import typer

from ..core.models import AppConfig, ConfigError
from ..core.paths import resolve_config_dir, resolve_config_path

EXIT_OK = 0
EXIT_USER_ERROR = 1
EXIT_RUNTIME_ERROR = 2
EXIT_DETECTOR_ERROR = 3
EXIT_INTERNAL_ERROR = 4


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
            print(f"错误: {error.get('message', '')}", file=sys.stderr)
        elif data is not None:
            _print_human(data)

    if exit_code != EXIT_OK:
        raise typer.Exit(exit_code)


def _print_human(data: Any) -> None:
    """人类可读输出"""
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, (dict, list)):
                print(f"{k}: {json.dumps(v, ensure_ascii=False)}")
            else:
                print(f"{k}: {v}")
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                print(json.dumps(item, ensure_ascii=False))
            else:
                print(item)
    elif isinstance(data, str):
        print(data)
    else:
        print(json.dumps(data, ensure_ascii=False))


# ============================================================
# 配置路径
# ============================================================


def get_config_path(ctx: typer.Context) -> Path:
    """从 ctx.obj 取配置路径（或环境变量 SAITEC_CONFIG / platformdirs 默认）

    注意：sub-typer 命令（config get/set/...）的 ctx.obj 不继承主 callback 的修改，
    所以这里显式回退到环境变量。
    """
    try:
        explicit = (ctx.obj or {}).get("config_path")
        if explicit is not None:
            return Path(explicit).expanduser().resolve()
    except (AttributeError, TypeError):
        pass
    env_var = os.environ.get("SAITEC_CONFIG")
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
    return config_path.parent / "safe-guard.pid"


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
    return config_path.parent / "logs" / "safe-guard.log"


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

    services 元素字段：name / port / upstream / endpoint_type（来自
    EndpointSpec asdict 或 config 解析后的 dict）。
    """
    if not services:
        return "服务映射: （无服务，用 `safe-guard service add` 添加）"
    lines = ["服务映射（客户端 base_url → 本地端口 → 真实上游）:"]
    for i, s in enumerate(services, 1):
        port = s.get("port", 0)
        port_str = str(port) if port else "自动分配"
        lines.append(
            f"  {i}. {s.get('name', '?')}  [{s.get('endpoint_type', '?')}]"
            f"  127.0.0.1:{port_str}  →  {s.get('upstream', '?')}"
        )
        lines.append(f"     客户端配置: {client_env_hint(s.get('endpoint_type', ''), port)}")
    return "\n".join(lines)
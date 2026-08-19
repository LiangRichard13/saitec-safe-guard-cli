"""init — 交互式 / 非交互式生成 config.json"""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import typer

from .._common import (
    EXIT_INTERNAL_ERROR,
    EXIT_USER_ERROR,
    emit,
    get_config_path,
)
from ...core.paths import ensure_dirs
from ...core.utils import now_iso8601

# 默认服务模板（一个端点一个默认服务）
DEFAULT_SERVICES = [
    {
        "name": "openai-chat-completions",
        "port": 9001,
        "upstream": "https://api.openai.com",
        "endpoint_type": "openai-chat-completions",
        "record_body": True,
    },
    {
        "name": "openai-responses",
        "port": 9002,
        "upstream": "https://api.openai.com",
        "endpoint_type": "openai-responses",
        "record_body": True,
    },
    {
        "name": "anthropic-messages",
        "port": 9003,
        "upstream": "https://api.anthropic.com",
        "endpoint_type": "anthropic-messages",
        "record_body": True,
    },
]


def _build_default_config(detector_url: str, api_key: str) -> dict:
    return {
        "config_version": 1,
        "detector": {
            "url": detector_url,
            "api_key": api_key,
            "report_interval_sec": 60,
            "batch_size": 500,
            "max_queue_size": 10000,
        },
        "services": DEFAULT_SERVICES,
        "log_level": "INFO",
    }


def _prompt(prompt: str, default: str = "", secret: bool = False) -> str:
    """交互式提示；非 TTY 时直接用默认值（便于自动化）"""
    if not sys.stdin.isatty():
        return default
    suffix = "" if not default else f" [{default}]"
    try:
        import getpass

        if secret:
            value = getpass.getpass(f"{prompt}{suffix}: ")
        else:
            value = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        return default
    return value or default


def _set_file_private(path: Path) -> None:
    """设置 config.json 仅当前用户可读写"""
    if os.name == "nt":
        # Windows：提示用户手动 icacls（自动设置需管理员）
        return
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def init_cmd(
    ctx: typer.Context,
    api_key: str | None = typer.Option(
        None,
        "--api-key",
        envvar="SAITEC_API_KEY",
        help="X-API-Key（建议从 stdin 注入，避免 shell history）",
    ),
    detector_url: str | None = typer.Option(
        None,
        "--detector-url",
        envvar="SAITEC_DETECTOR_URL",
        help="检测服务器地址",
    ),
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
    force: bool = typer.Option(False, "--force", help="覆盖已存在的 config.json"),
) -> None:
    """生成 config.json（默认到 platformdirs 用户目录；可显式 --config 覆盖）"""
    path = config_path.expanduser().resolve() if config_path else get_config_path(ctx)

    if path.exists() and not force:
        emit(
            json_output=json_output,
            ok=False,
            error={
                "code": "CONFIG_EXISTS",
                "message": f"config.json 已存在: {path}（用 --force 覆盖）",
            },
            exit_code=EXIT_USER_ERROR,
        )
        return

    # 1. detector_url：--detector-url > 交互 > 默认
    if detector_url is None:
        detector_url = _prompt("检测服务器地址 (detector URL)", default="http://detector:8080")
    # 2. api_key：--api-key > 交互
    if api_key is None:
        api_key = _prompt("X-API-Key", secret=True)

    if not api_key:
        emit(
            json_output=json_output,
            ok=False,
            error={
                "code": "MISSING_API_KEY",
                "message": "缺少 api_key：用 --api-key 指定或通过环境变量 SAITEC_API_KEY 提供",
            },
            exit_code=EXIT_USER_ERROR,
        )
        return

    config = _build_default_config(detector_url, api_key)

    try:
        ensure_dirs()
        path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
        _set_file_private(path)
    except OSError as e:
        emit(
            json_output=json_output,
            ok=False,
            error={"code": "WRITE_ERROR", "message": f"写入失败: {e}"},
            exit_code=EXIT_INTERNAL_ERROR,
        )
        return

    emit(
        json_output=json_output,
        data={
            "config_path": str(path),
            "detector_url": detector_url,
            "services": len(config["services"]),
            "created_at": now_iso8601(),
            "warning": "建议在 Windows 上用 icacls 限制 config.json 权限" if os.name == "nt" else None,
        },
    )
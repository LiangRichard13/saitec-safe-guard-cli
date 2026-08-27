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
    OK,
    WARN,
    console,
    emit,
    err_console,
    format_services_block,
    get_config_path,
)
from ...core.config import guess_endpoint_type, upstream_endpoint_warning
from ...core.paths import ensure_dirs
from ...core.utils import now_iso8601

_VALID_ENDPOINT_TYPES = (
    "openai-chat-completions",
    "openai-responses",
    "anthropic-messages",
)


def _build_default_config(
    detector_url: str,
    api_key: str,
    service: dict,
) -> dict:
    return {
        "config_version": 1,
        "detector": {
            "url": detector_url,
            "api_key": api_key,
            "endpoint_path": "/detect",
            "report_interval_sec": 60,
            "batch_size": 500,
            "max_queue_size": 10000,
        },
        "services": [service],
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
        envvar="SSGC_API_KEY",
        help="X-API-Key（建议从 stdin 注入，避免 shell history）",
    ),
    detector_url: str | None = typer.Option(
        None,
        "--detector-url",
        envvar="SSGC_DETECTOR_URL",
        help="检测服务器地址",
    ),
    upstream: str | None = typer.Option(
        None,
        "--upstream",
        "-u",
        help="要监控的上游 base URL（如 https://api.deepseek.com/anthropic、http://localhost:23333）",
    ),
    endpoint_type: str | None = typer.Option(
        None,
        "--endpoint-type",
        "-t",
        help=f"协议格式：{' / '.join(_VALID_ENDPOINT_TYPES)}（缺省按 upstream URL 猜测）",
    ),
    name: str | None = typer.Option(
        None, "--name", help="服务名（缺省用 endpoint_type）"
    ),
    port: int = typer.Option(
        9001, "--port", "-p", help="本地监听端口（默认 9001）"
    ),
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
    force: bool = typer.Option(False, "--force", help="覆盖已存在的 config.json"),
) -> None:
    """🎯 生成 config.json（单服务起步）

    交互式（TTY）或非交互式生成 `~/.ssgc/config.json`。首次使用必跑的命令。
    生成单服务配置；要监控更多端点用 `ssgc service add`。

    校验：api_key ≥ 8 字符；detector-url 与 upstream 必须以 http/https 开头；
    非 TTY 且未给 `--upstream` 时报错（upstream 必须显式指定）。

    \b
    Examples:
      ssgc init --api-key KEY --detector-url http://detector:8080 --upstream https://api.deepseek.com
      ssgc init --api-key KEY --detector-url URL --upstream http://localhost:23333 --port 9010
      ssgc init --api-key KEY --detector-url URL --upstream URL --force  # 覆盖已有

    \b
    Troubleshooting:
      • config.json 已存在 → 加 `--force` 或先 `ssgc service add` 加新服务
      • api_key 长度 < 8 → 报错；拼错上线会 401 才能发现
      • upstream 写成完整 URL（`.../v1/chat/completions`）→ 警告但不阻止，建议改回 base URL

    \b
    See also:
      `ssgc service add` 加监控端点   `ssgc validate` 校验配置
      `ssgc start` 启动服务            `docs/user-guide.md` §3 快速上手
    """
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
    # 3. upstream：--upstream > 交互（TTY）> 报错（非 TTY）
    if upstream is None:
        upstream = _prompt(
            "要监控的上游 base URL (upstream)\n"
            "  例: https://api.openai.com / https://api.deepseek.com/anthropic / http://localhost:23333"
        )

    if not api_key:
        emit(
            json_output=json_output,
            ok=False,
            error={
                "code": "MISSING_API_KEY",
                "message": "缺少 api_key：用 --api-key 指定或通过环境变量 SSGC_API_KEY 提供",
            },
            exit_code=EXIT_USER_ERROR,
        )
        return

    if not upstream:
        emit(
            json_output=json_output,
            ok=False,
            error={
                "code": "MISSING_UPSTREAM",
                "message": "缺少 upstream：用 --upstream 指定要监控的上游 base URL"
                          "（如 https://api.deepseek.com/anthropic、http://localhost:23333）",
            },
            exit_code=EXIT_USER_ERROR,
        )
        return

    # 简单格式校验：避免空白 / 极短字符串写入配置后才发现
    if not detector_url or not detector_url.startswith(("http://", "https://")):
        emit(
            json_output=json_output,
            ok=False,
            error={
                "code": "INVALID_DETECTOR_URL",
                "message": f"detector URL 必须以 http:// 或 https:// 开头: {detector_url!r}",
            },
            exit_code=EXIT_USER_ERROR,
        )
        return
    if not upstream.startswith(("http://", "https://")):
        emit(
            json_output=json_output,
            ok=False,
            error={
                "code": "INVALID_UPSTREAM",
                "message": f"upstream 必须以 http:// 或 https:// 开头: {upstream!r}",
            },
            exit_code=EXIT_USER_ERROR,
        )
        return
    api_key_stripped = api_key.strip()
    if len(api_key_stripped) < 8:
        emit(
            json_output=json_output,
            ok=False,
            error={
                "code": "INVALID_API_KEY",
                "message": f"api_key 长度过短（{len(api_key_stripped)} < 8），请确认是否填写完整",
            },
            exit_code=EXIT_USER_ERROR,
        )
        return
    api_key = api_key_stripped

    # endpoint_type：显式 > URL 启发式
    guessed = False
    if endpoint_type is None:
        endpoint_type = guess_endpoint_type(upstream)
        guessed = True
    if endpoint_type not in _VALID_ENDPOINT_TYPES:
        emit(
            json_output=json_output,
            ok=False,
            error={
                "code": "INVALID_ENDPOINT_TYPE",
                "message": f"endpoint_type 必须是 {' / '.join(_VALID_ENDPOINT_TYPES)}，got {endpoint_type!r}",
            },
            exit_code=EXIT_USER_ERROR,
        )
        return

    service = {
        "name": name or endpoint_type,
        "port": port,
        "upstream": upstream,
        "endpoint_type": endpoint_type,
        "record_body": True,
    }
    config = _build_default_config(detector_url, api_key, service)

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

    warnings: list[str] = []
    w = upstream_endpoint_warning(upstream)
    if w:
        warnings.append(w)
        if not json_output:
            err_console.print(f"{WARN} {w}")

    if json_output:
        emit(
            json_output=True,
            data={
                "config_path": str(path),
                "detector_url": detector_url,
                "services": [service],
                "endpoint_type_guessed": guessed,
                "warnings": warnings,
                "created_at": now_iso8601(),
                "next_steps": [
                    "监控更多端点: ssgc service add <name> --upstream <URL>",
                    "启动服务: ssgc start",
                ],
            },
        )
    else:
        console.print(f"{OK} 配置已生成  [dim]{path}[/dim]")
        console.print(f"[dim]检测服务器:[/dim] {detector_url}")
        console.print(f"[dim]生成时间:[/dim] {now_iso8601()}")
        if guessed:
            console.print(f"[dim]协议格式:[/dim] [cyan]{endpoint_type}[/cyan] [dim]（按 upstream URL 猜测，可用 --endpoint-type 显式指定）[/dim]")
        console.print()
        console.print(format_services_block([service]))
        console.print()
        console.print("[dim]下一步:[/dim]")
        console.print(f"  - 监控更多端点: [cyan]ssgc service add <name> --upstream <URL>[/cyan]")
        console.print(f"  - 启动服务:     [cyan]ssgc start[/cyan]")
        if os.name == "nt":
            err_console.print(f"{WARN} 建议在 Windows 上用 icacls 限制 config.json 权限")
"""validate — 校验 config.json（含三级覆盖，不启动服务）"""
from __future__ import annotations

from pathlib import Path

import typer

from .._common import (
    EXIT_OK,
    EXIT_USER_ERROR,
    emit,
    format_errors,
    get_config_path,
)
from ...core.config import apply_cli_overrides, apply_env_overrides, load_config_json, validate_config


def validate_cmd(
    ctx: typer.Context,
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """校验 config.json（含三级覆盖），不启动服务"""
    path = config_path.expanduser().resolve() if config_path else get_config_path(ctx)

    try:
        config = load_config_json(path)
        config = apply_env_overrides(config)
    except FileNotFoundError:
        emit(
            json_output=json_output,
            ok=False,
            error={"code": "CONFIG_NOT_FOUND", "message": f"config.json 不存在: {path}"},
            exit_code=EXIT_USER_ERROR,
        )
        return
    except (ValueError, KeyError) as e:
        emit(
            json_output=json_output,
            ok=False,
            error={"code": "CONFIG_PARSE_ERROR", "message": str(e)},
            exit_code=EXIT_USER_ERROR,
        )
        return

    errors = validate_config(config)
    if errors:
        emit(
            json_output=json_output,
            ok=False,
            error={
                "code": "CONFIG_VALIDATION_ERROR",
                "message": f"配置校验失败: {len(errors)} 个错误",
                "errors": format_errors(errors),
            },
            exit_code=EXIT_USER_ERROR,
        )
        return

    emit(
        json_output=json_output,
        data={
            "valid": True,
            "config_path": str(path),
            "services": len(config.services),
            "detector_url": config.detector.url,
        },
    )
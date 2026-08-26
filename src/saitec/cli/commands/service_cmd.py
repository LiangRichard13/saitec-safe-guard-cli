"""service — 监控服务的增删改查（add / remove / set / list）

管理 config.json 里的 services 数组：要监控哪些上游端点、各用什么协议
格式、本地监听哪个端口。改完需 `safe-guard restart` 生效。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from .._common import (
    EXIT_USER_ERROR,
    WARN,
    emit,
    err_console,
    format_services_block,
    get_config_path,
)
from ...core.config import (
    guess_endpoint_type,
    upstream_endpoint_warning,
    validate_config,
)
from .config_cmd import _load_raw, _raw_to_model, _save_raw

app = typer.Typer(help="监控服务管理（add / remove / set / list）")

_PORT_START = 9001


def _emit_warnings(json_output: bool, warnings: list[str]) -> None:
    """警告走 stderr（人类可读黄色图标）或并入 data（JSON 模式由调用方拼）"""
    if not json_output:
        for w in warnings:
            err_console.print(f"{WARN} {w}")


def _load_or_emit(path: Path, json_output: bool) -> dict | None:
    """读取 config，不存在时 emit 错误并返回 None"""
    try:
        return _load_raw(path)
    except FileNotFoundError:
        emit(json_output=json_output, ok=False,
             error={"code": "CONFIG_NOT_FOUND", "message": f"config.json 不存在: {path}"},
             exit_code=EXIT_USER_ERROR)
        return None
    except ValueError as e:
        emit(json_output=json_output, ok=False,
             error={"code": "CONFIG_PARSE_ERROR", "message": str(e)},
             exit_code=EXIT_USER_ERROR)
        return None


def _find_service(data: dict, name: str) -> dict | None:
    for s in data.get("services", []):
        if s.get("name") == name:
            return s
    return None


def _next_free_port(data: dict) -> int:
    """从 9001 起找未被其它服务占用的端口（确定性，避免 port=0 每次变）"""
    used = {s.get("port", 0) for s in data.get("services", [])}
    port = _PORT_START
    while port in used:
        port += 1
    return port


def _validate_and_save(
    path: Path, data: dict, json_output: bool
) -> bool:
    """校验 + 备份写入；校验失败 emit 错误返回 False"""
    try:
        model = _raw_to_model(data)
    except (KeyError, ValueError, TypeError) as e:
        emit(json_output=json_output, ok=False,
             error={"code": "INVALID_CONFIG", "message": f"配置不完整: {e}"},
             exit_code=EXIT_USER_ERROR)
        return False
    errors = validate_config(model)
    if errors:
        emit(json_output=json_output, ok=False,
             error={
                 "code": "VALIDATION_FAILED",
                 "message": "修改会导致配置无效（未写入）",
                 "errors": [
                     {"code": e.code.value, "field": e.field, "message": e.message}
                     for e in errors
                 ],
             },
             exit_code=EXIT_USER_ERROR)
        return False
    try:
        _save_raw(path, data)
    except OSError as e:
        emit(json_output=json_output, ok=False,
             error={"code": "WRITE_ERROR", "message": f"写入失败: {e}"},
             exit_code=EXIT_USER_ERROR)
        return False
    return True


# ============================================================
# list
# ============================================================


@app.command(name="list")
def list_cmd(
    ctx: typer.Context,
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """列出所有监控服务（客户端 base_url → 本地端口 → 真实上游）"""
    path = config_path.expanduser().resolve() if config_path else get_config_path(ctx)
    data = _load_or_emit(path, json_output)
    if data is None:
        return
    services = data.get("services", [])
    if json_output:
        emit(json_output=True, data={
            "count": len(services),
            "services": services,
            "note": "修改后需 `safe-guard restart` 生效",
        })
    else:
        emit(json_output=False, data=format_services_block(services))


# ============================================================
# add
# ============================================================


@app.command(name="add")
def add_cmd(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="服务名（唯一标识，用于日志过滤）"),
    upstream: str = typer.Option(
        ...,
        "--upstream",
        "-u",
        envvar=None,
        help="上游 base URL（如 https://api.deepseek.com/anthropic、http://localhost:23333）",
    ),
    endpoint_type: str = typer.Option(
        None,
        "--endpoint-type",
        "-t",
        help="协议格式：openai-chat-completions / openai-responses / anthropic-messages（缺省按 URL 猜测）",
    ),
    port: int = typer.Option(
        None, "--port", "-p", help="本地监听端口（缺省从 9001 起自动分配空闲端口）"
    ),
    record_body: bool = typer.Option(True, "--record-body/--no-record-body",
                                     help="是否记录请求/响应体"),
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """添加一个监控服务（转发到指定上游端点）"""
    path = config_path.expanduser().resolve() if config_path else get_config_path(ctx)
    data = _load_or_emit(path, json_output)
    if data is None:
        return

    if _find_service(data, name) is not None:
        emit(json_output=json_output, ok=False,
             error={"code": "NAME_EXISTS", "message": f"服务名已存在: {name}"},
             exit_code=EXIT_USER_ERROR)
        return

    guessed = False
    if endpoint_type is None:
        endpoint_type = guess_endpoint_type(upstream)
        guessed = True
    if port is None:
        port = _next_free_port(data)

    warnings: list[str] = []
    w = upstream_endpoint_warning(upstream)
    if w:
        warnings.append(w)

    svc = {
        "name": name,
        "port": port,
        "upstream": upstream,
        "endpoint_type": endpoint_type,
        "record_body": record_body,
    }
    data.setdefault("services", []).append(svc)
    if not _validate_and_save(path, data, json_output):
        return

    _emit_warnings(json_output, warnings)
    result: dict[str, Any] = {
        "added": name,
        "port": port,
        "upstream": upstream,
        "endpoint_type": endpoint_type,
        "endpoint_type_guessed": guessed,
        "record_body": record_body,
        "note": "重启后生效: safe-guard restart",
    }
    if warnings and json_output:
        result["warnings"] = warnings
    emit(json_output=json_output, data=result)


# ============================================================
# remove
# ============================================================


@app.command(name="remove")
def remove_cmd(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="要移除的服务名"),
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """移除一个监控服务"""
    path = config_path.expanduser().resolve() if config_path else get_config_path(ctx)
    data = _load_or_emit(path, json_output)
    if data is None:
        return

    svc = _find_service(data, name)
    if svc is None:
        emit(json_output=json_output, ok=False,
             error={"code": "NAME_NOT_FOUND", "message": f"服务不存在: {name}"},
             exit_code=EXIT_USER_ERROR)
        return

    data["services"] = [s for s in data.get("services", []) if s.get("name") != name]
    if not _validate_and_save(path, data, json_output):
        return
    emit(json_output=json_output,
         data={"removed": name, "remaining": len(data["services"]),
               "note": "重启后生效: safe-guard restart"})


# ============================================================
# set
# ============================================================


@app.command(name="set")
def set_cmd(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="服务名"),
    upstream: str | None = typer.Option(None, "--upstream", "-u", help="新上游 base URL"),
    endpoint_type: str | None = typer.Option(None, "--endpoint-type", "-t",
                                             help="新协议格式"),
    port: int | None = typer.Option(None, "--port", "-p", help="新本地端口"),
    record_body: bool | None = typer.Option(None, "--record-body/--no-record-body",
                                            help="是否记录请求/响应体"),
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """修改一个监控服务的字段（至少给一项）"""
    path = config_path.expanduser().resolve() if config_path else get_config_path(ctx)
    data = _load_or_emit(path, json_output)
    if data is None:
        return

    svc = _find_service(data, name)
    if svc is None:
        emit(json_output=json_output, ok=False,
             error={"code": "NAME_NOT_FOUND", "message": f"服务不存在: {name}"},
             exit_code=EXIT_USER_ERROR)
        return

    changes: dict[str, Any] = {}
    if upstream is not None:
        svc["upstream"] = upstream
        changes["upstream"] = upstream
    if endpoint_type is not None:
        svc["endpoint_type"] = endpoint_type
        changes["endpoint_type"] = endpoint_type
    if port is not None:
        svc["port"] = port
        changes["port"] = port
    if record_body is not None:
        svc["record_body"] = record_body
        changes["record_body"] = record_body

    if not changes:
        emit(json_output=json_output, ok=False,
             error={"code": "NO_CHANGES",
                    "message": "未指定任何修改（用 --upstream / --port / --endpoint-type / --record-body）"},
             exit_code=EXIT_USER_ERROR)
        return

    warnings: list[str] = []
    w = upstream_endpoint_warning(svc["upstream"])
    if w:
        warnings.append(w)

    if not _validate_and_save(path, data, json_output):
        return

    _emit_warnings(json_output, warnings)
    result: dict[str, Any] = {"changed": name, "changes": changes,
                              "note": "重启后生效: safe-guard restart"}
    if warnings and json_output:
        result["warnings"] = warnings
    emit(json_output=json_output, data=result)

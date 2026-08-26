"""config — 配置管理（get / set / unset / list）

详见 `docs/design/saitec-safe-guard-cli-design.md` §15。

基于原始 JSON 的 dot-path 操作（非 dataclass），便于持久化修改。
"""
from __future__ import annotations

import copy
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import typer

from .._common import (
    EXIT_OK,
    EXIT_USER_ERROR,
    WARN,
    emit,
    err_console,
    format_errors,
    get_config_path,
)
from ...core.config import upstream_endpoint_warning, validate_config
from ...core.models import AppConfig, DetectorConfig, EndpointSpec
from ...core.utils import now_iso8601

app = typer.Typer(help="配置管理（get / set / unset / list）")

# 需要转换类型的字段
_INT_FIELDS = {
    "detector.report_interval_sec",
    "detector.batch_size",
    "detector.max_queue_size",
    "port",
}
_BOOL_FIELDS = {"record_body"}

# 敏感字段：任何输出（list / get）都不得展示明文
_SECRET_FIELDS = {"detector.api_key", "api_key"}


def _redact(value: Any, field: str) -> Any:
    """敏感字段脱敏：api_key 显示 sk-***"""
    if field in _SECRET_FIELDS or field.endswith(".api_key"):
        if value and isinstance(value, str):
            return value[:3] + "***" if len(value) > 3 else "***"
        return "***"
    return value


def _load_raw(path: Path) -> dict:
    """读取原始 config JSON"""
    return json.loads(path.read_text(encoding="utf-8"))


def _save_raw(path: Path, data: dict) -> None:
    """写入 + 快照"""
    if path.exists():
        backup = path.with_name(f"config.json.bak.{datetime.now().strftime('%Y%m%d%H%M%S')}")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _resolve_key_path(key: str) -> list[str]:
    """把 dot path 解析为层级列表"""
    return key.split(".")


def _get_by_path(data: dict, parts: list[str]) -> Any:
    cur: Any = data
    for p in parts:
        if isinstance(cur, dict):
            if p not in cur:
                raise KeyError(p)
            cur = cur[p]
        elif isinstance(cur, list):
            # services.<name> 支持按 name 匹配
            matched = [x for x in cur if isinstance(x, dict) and x.get("name") == p]
            if not matched:
                raise KeyError(p)
            cur = matched[0]
        else:
            raise KeyError(p)
    return cur


def _set_by_path(data: dict, parts: list[str], value: Any) -> None:
    """按 dot path 写值（中途不存在则报错，不允许创建新数组）"""
    cur: Any = data
    for i, p in enumerate(parts):
        last = i == len(parts) - 1
        if isinstance(cur, dict):
            if last:
                cur[p] = value
                return
            if p not in cur or not isinstance(cur[p], (dict, list)):
                raise KeyError(f"中间层级不存在: {'.'.join(parts[:i+1])}")
            cur = cur[p]
        elif isinstance(cur, list):
            matched = [x for x in cur if isinstance(x, dict) and x.get("name") == p]
            if not matched:
                raise KeyError(f"未找到服务: {p}")
            cur = matched[0]
        else:
            raise KeyError(f"中间层级不是对象: {'.'.join(parts[:i+1])}")


def _unset_by_path(data: dict, parts: list[str]) -> bool:
    cur: Any = data
    for i, p in enumerate(parts):
        last = i == len(parts) - 1
        if isinstance(cur, dict):
            if last:
                if p in cur:
                    del cur[p]
                    return True
                return False
            if p not in cur:
                return False
            cur = cur[p]
        elif isinstance(cur, list):
            matched = [x for x in cur if isinstance(x, dict) and x.get("name") == p]
            if not matched:
                return False
            cur = matched[0]
        else:
            return False
    return False


def _coerce_value(key: str, raw: str) -> Any:
    """按字段类型转换值"""
    key_lower = key.lower()
    if key_lower in _INT_FIELDS or key_lower.endswith(".port"):
        return int(raw)
    if key_lower in _BOOL_FIELDS or key_lower.endswith(".record_body"):
        return raw.lower() in ("1", "true", "yes", "on")
    return raw


def _raw_to_model(data: dict) -> AppConfig:
    """把原始 dict 转成 AppConfig 以便复用 validate_config"""
    detector = DetectorConfig(
        url=data.get("detector", {}).get("url", ""),
        api_key=data.get("detector", {}).get("api_key", ""),
        endpoint_path=data.get("detector", {}).get("endpoint_path", "/detect"),
        report_interval_sec=data.get("detector", {}).get("report_interval_sec", 60),
        batch_size=data.get("detector", {}).get("batch_size", 500),
        max_queue_size=data.get("detector", {}).get("max_queue_size", 10000),
    )
    services = [
        EndpointSpec(
            name=s.get("name", ""),
            port=s.get("port", 0),
            upstream=s.get("upstream", ""),
            endpoint_type=s.get("endpoint_type", ""),
            record_body=s.get("record_body", True),
        )
        for s in data.get("services", [])
    ]
    return AppConfig(
        detector=detector,
        services=services,
        log_level=data.get("log_level", "INFO"),
    )


def _collect_flat(data: dict, prefix: str = "") -> dict[str, Any]:
    """扁平化 config → {dot_path: value}"""
    out: dict[str, Any] = {}
    for k, v in data.items():
        path = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_collect_flat(v, path))
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict) and "name" in item:
                    out.update(_collect_flat(item, f"{path}.{item['name']}"))
                else:
                    out[f"{path}.[]"] = item
        else:
            out[path] = v
    return out


@app.command(name="get")
def get_cmd(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="字段路径，如 detector.url / services.<name>.port"),
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """查看单个字段（按点路径）"""
    path = config_path.expanduser().resolve() if config_path else get_config_path(ctx)
    try:
        data = _load_raw(path)
    except FileNotFoundError:
        emit(json_output=json_output, ok=False,
             error={"code": "CONFIG_NOT_FOUND", "message": f"config.json 不存在: {path}"},
             exit_code=EXIT_USER_ERROR)
        return
    try:
        value = _get_by_path(data, _resolve_key_path(key))
    except (KeyError, IndexError) as e:
        emit(json_output=json_output, ok=False,
             error={"code": "KEY_NOT_FOUND", "message": f"字段不存在: {key}"},
             exit_code=EXIT_USER_ERROR)
        return
    # 敏感字段（api_key）脱敏后输出
    emit(json_output=json_output, data={"key": key, "value": _redact(value, key)})


@app.command(name="set")
def set_cmd(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="字段路径"),
    value: str = typer.Argument(..., help="字段值"),
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """修改单个字段（自动快照 + 校验 + 不自动重启）"""
    path = config_path.expanduser().resolve() if config_path else get_config_path(ctx)
    try:
        data = _load_raw(path)
    except FileNotFoundError:
        emit(json_output=json_output, ok=False,
             error={"code": "CONFIG_NOT_FOUND", "message": f"config.json 不存在: {path}"},
             exit_code=EXIT_USER_ERROR)
        return

    try:
        parsed = _coerce_value(key, value)
        _set_by_path(data, _resolve_key_path(key), parsed)
    except (KeyError, ValueError) as e:
        emit(json_output=json_output, ok=False,
             error={"code": "SET_FAILED", "message": str(e)},
             exit_code=EXIT_USER_ERROR)
        return

    key_lower = key.lower()

    # 校验
    try:
        model = _raw_to_model(data)
    except (KeyError, ValueError, TypeError) as e:
        emit(json_output=json_output, ok=False,
             error={"code": "INVALID_CONFIG", "message": f"配置不完整: {e}"},
             exit_code=EXIT_USER_ERROR)
        return
    errors = validate_config(model)
    if errors:
        emit(json_output=json_output, ok=False,
             error={
                 "code": "VALIDATION_FAILED",
                 "message": f"修改会导致配置无效（未写入）: {len(errors)} 个错误",
                 "errors": format_errors(errors),
             },
             exit_code=EXIT_USER_ERROR)
        return

    try:
        _save_raw(path, data)
    except OSError as e:
        emit(json_output=json_output, ok=False,
             error={"code": "WRITE_ERROR", "message": f"写入失败: {e}"},
             exit_code=EXIT_USER_ERROR)
        return

    # upstream 防呆：检测误配成完整端点 URL（路径重复 → 404）
    warnings: list[str] = []
    if key_lower.endswith(".upstream") and isinstance(parsed, str):
        w = upstream_endpoint_warning(parsed)
        if w:
            warnings.append(w)
            if not json_output:
                err_console.print(f"{WARN} {w}")

    result: dict[str, Any] = {"key": key, "value": parsed, "saved": True,
                              "note": "修改已保存。重启后生效（safe-guard restart）"}
    if warnings and json_output:
        result["warnings"] = warnings
    emit(json_output=json_output, data=result)


@app.command(name="unset")
def unset_cmd(
    ctx: typer.Context,
    key: str = typer.Argument(..., help="字段路径"),
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """清除字段（回退到默认）"""
    path = config_path.expanduser().resolve() if config_path else get_config_path(ctx)
    try:
        data = _load_raw(path)
    except FileNotFoundError:
        emit(json_output=json_output, ok=False,
             error={"code": "CONFIG_NOT_FOUND", "message": f"config.json 不存在: {path}"},
             exit_code=EXIT_USER_ERROR)
        return

    removed = _unset_by_path(data, _resolve_key_path(key))
    if not removed:
        emit(json_output=json_output, ok=False,
             error={"code": "KEY_NOT_FOUND", "message": f"字段不存在: {key}"},
             exit_code=EXIT_USER_ERROR)
        return

    _save_raw(path, data)
    emit(json_output=json_output,
         data={"key": key, "unset": True, "note": "该字段已移除，将使用默认值"})


@app.command(name="list")
def list_cmd(
    ctx: typer.Context,
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """列出所有字段（含来源：config / env / cli / default）"""
    path = config_path.expanduser().resolve() if config_path else get_config_path(ctx)
    try:
        data = _load_raw(path)
    except FileNotFoundError:
        emit(json_output=json_output, ok=False,
             error={"code": "CONFIG_NOT_FOUND", "message": f"config.json 不存在: {path}"},
             exit_code=EXIT_USER_ERROR)
        return

    flat = _collect_flat(data)

    # 环境变量来源标记 + 敏感字段脱敏
    env = os.environ
    for field in list(flat.keys()):
        env_var = _env_var_for_field(field)
        if env_var and env_var in env:
            flat[field] = env[env_var]  # 显示 env 覆盖后的值
        flat[field] = _redact(flat[field], field)

    if json_output:
        result = {
            "config": {
                k: {"value": v, "source": _source_for(k, env)}
                for k, v in flat.items()
            },
            "effective_at": now_iso8601(),
        }
        emit(json_output=True, data=result)
    else:
        # 人类可读表格
        lines = ["KEY\tVALUE\tSOURCE"]
        for k, v in flat.items():
            lines.append(f"{k}\t{v}\t{_source_for(k, env)}")
        emit(json_output=False, data="\n".join(lines))


def _env_var_for_field(field: str) -> str | None:
    """字段 → 环境变量名（若可覆盖）"""
    mapping = {
        "detector.url": "SAITEC_DETECTOR_URL",
        "detector.api_key": "SAITEC_API_KEY",
        "detector.endpoint_path": "SAITEC_ENDPOINT_PATH",
        "detector.report_interval_sec": "SAITEC_REPORT_INTERVAL",
        "detector.batch_size": "SAITEC_BATCH_SIZE",
        "detector.max_queue_size": "SAITEC_MAX_QUEUE_SIZE",
        "log_level": "SAITEC_LOG_LEVEL",
    }
    if field in mapping:
        return mapping[field]
    # services.<name>.<suffix>
    m = re.match(r"^services\.(.+)\.(port|upstream|record_body)$", field)
    if m:
        return f"SAITEC_{m.group(1).upper()}_{m.group(2).upper()}"
    return None


def _source_for(field: str, env: dict) -> str:
    env_var = _env_var_for_field(field)
    if env_var and env_var in env:
        return "env"
    return "config"
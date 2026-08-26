"""配置 schema + 校验 + 三级覆盖（详见 docs/design/saitec-safe-guard-cli-design.md §12.1）

仅依赖标准库，无 IO 库（文件读取、HTTP、数据库都交给调用方）。
"""
from __future__ import annotations

import copy
import dataclasses
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .models import (
    AppConfig,
    ConfigError,
    ConfigErrorCode,
    ConfigSource,
    ConfigSources,
    ConfigValidationError,
    DetectorConfig,
    EndpointSpec,
)

# 环境变量映射：env_var → ((section, field), type)
# section=None 表示顶层字段
_DETECTOR_ENV_MAP: dict[str, tuple[tuple[str, str], type]] = {
    "SSGC_DETECTOR_URL": (("detector", "url"), str),
    "SSGC_API_KEY": (("detector", "api_key"), str),
    "SSGC_ENDPOINT_PATH": (("detector", "endpoint_path"), str),
    "SSGC_REPORT_INTERVAL": (("detector", "report_interval_sec"), int),
    "SSGC_BATCH_SIZE": (("detector", "batch_size"), int),
    "SSGC_MAX_QUEUE_SIZE": (("detector", "max_queue_size"), int),
}
_TOP_LEVEL_ENV_MAP: dict[str, tuple[tuple[str], type]] = {
    "SSGC_LOG_LEVEL": (("log_level",), str),
}

# CLI 参数映射：kwarg → ((section, field), type)
_CLI_FIELD_MAP: dict[str, tuple[tuple[str, ...], type]] = {
    "detector_url": (("detector", "url"), str),
    "api_key": (("detector", "api_key"), str),
    "endpoint_path": (("detector", "endpoint_path"), str),
    "report_interval": (("detector", "report_interval_sec"), int),
    "batch_size": (("detector", "batch_size"), int),
    "max_queue_size": (("detector", "max_queue_size"), int),
    "log_level": (("log_level",), str),
}

# service 维度通过 env 覆盖时的字段后缀
_SERVICE_FIELD_BY_SUFFIX = {
    "PORT": ("port", int),
    "UPSTREAM": ("upstream", str),
    "RECORD_BODY": ("record_body", bool),
}

_VALID_ENDPOINT_TYPES = frozenset(
    {"openai-chat-completions", "openai-responses", "anthropic-messages"}
)

# upstream 末尾的端点特征后缀（配了会导致转发路径重复）
_ENDPOINT_PATH_SUFFIXES = (
    "/chat/completions",
    "/completions",
    "/messages",
    "/responses",
)


def upstream_endpoint_warning(url: str) -> str | None:
    """检测 upstream 是否误配成完整端点 URL（转发会路径重复）

    upstream 语义是 URL 前缀：完整转发地址 = upstream + 客户端请求路径。
    若 upstream 以 /chat/completions 等端点后缀结尾，客户端再带一遍路径
    就会重复（如 /v1/chat/completions/v1/chat/completions）→ 404。
    返回警告文案；无问题返回 None。仅警告不阻断（存在路径真的长这样的网关）。
    """
    path = urlparse(url).path.rstrip("/")
    for suffix in _ENDPOINT_PATH_SUFFIXES:
        if path.endswith(suffix):
            return (
                f"upstream 末尾的 '{suffix}' 疑似端点路径：upstream 是 base URL 前缀，"
                f"客户端请求路径会拼在它后面，可能导致路径重复（404）。"
                f"如真实端点就是 '{url}'，请改成去掉 '{suffix}' 的前缀形式"
            )
    return None


def guess_endpoint_type(upstream_url: str) -> str:
    """按 upstream URL 启发式猜测 endpoint_type

    含 'anthropic' → anthropic-messages；否则默认 openai-chat-completions
    （OpenAI 兼容格式最通用，绝大多数中转站/本地模型走它）。
    """
    lowered = upstream_url.lower()
    if "anthropic" in lowered:
        return "anthropic-messages"
    return "openai-chat-completions"


# ============================================================
# 内部辅助
# ============================================================


def _set_field(obj: Any, path: tuple[str, ...], value: Any) -> None:
    """按路径设置 dataclass 字段"""
    for name in path[:-1]:
        obj = getattr(obj, name)
    setattr(obj, path[-1], value)


def _cast_value(raw: str, type_: type) -> Any:
    """按目标类型转换字符串"""
    if type_ is bool:
        return raw.lower() in ("1", "true", "yes", "on")
    if type_ is int:
        return int(raw)
    if type_ is str:
        return raw
    raise ValueError(f"unsupported type: {type_}")


def _diff_changed_paths(old: AppConfig, new: AppConfig) -> list[str]:
    """对比新旧 config，输出字段路径列表"""
    changed: list[str] = []
    old_d, new_d = dataclasses.asdict(old), dataclasses.asdict(new)  # type: ignore[arg-type]
    if old_d.get("detector") != new_d.get("detector"):
        for k in old_d["detector"]:
            if old_d["detector"][k] != new_d["detector"][k]:
                changed.append(f"detector.{k}")
    if old_d.get("log_level") != new_d.get("log_level"):
        changed.append("log_level")
    old_svcs, new_svcs = old_d.get("services", []), new_d.get("services", [])
    if len(old_svcs) == len(new_svcs):
        for i, (o, n) in enumerate(zip(old_svcs, new_svcs)):
            for k in o:
                if o[k] != n[k]:
                    name = o.get("name", f"[{i}]")
                    changed.append(f"services.{name}.{k}")
    return changed


# ============================================================
# 公开 API
# ============================================================


def load_config_json(path: Path) -> AppConfig:
    """从 `config.json` 加载完整 AppConfig（不应用 env / cli 覆盖）

    Raises:
        FileNotFoundError: 文件不存在
        ValueError: JSON 损坏或字段缺失
    """
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON in {path}: {e}") from e

    detector_raw = data["detector"]
    detector = DetectorConfig(
        url=detector_raw["url"],
        api_key=detector_raw["api_key"],
        endpoint_path=detector_raw.get("endpoint_path", "/detect"),
        report_interval_sec=detector_raw.get("report_interval_sec", 60),
        batch_size=detector_raw.get("batch_size", 500),
        max_queue_size=detector_raw.get("max_queue_size", 10000),
    )
    services = [
        EndpointSpec(
            name=s["name"],
            port=s["port"],
            upstream=s["upstream"],
            endpoint_type=s["endpoint_type"],
            record_body=s.get("record_body", True),
        )
        for s in data.get("services", [])
    ]
    return AppConfig(
        detector=detector,
        services=services,
        log_level=data.get("log_level", "INFO"),
    )


def apply_env_overrides(config: AppConfig) -> AppConfig:
    """应用 `SSGC_*` 环境变量覆盖

    支持：
    - `SSGC_DETECTOR_*` 覆盖 detector 字段
    - `SSGC_LOG_LEVEL` 覆盖顶层 log_level
    - `SSGC_<NAME>_PORT/UPSTREAM/RECORD_BODY` 覆盖 service 字段
    """
    new_config = copy.deepcopy(config)
    env = os.environ

    # detector 维度
    for env_var, (path, type_) in _DETECTOR_ENV_MAP.items():
        if env_var in env:
            _set_field(new_config, path, _cast_value(env[env_var], type_))

    # 顶层
    for env_var, (path, type_) in _TOP_LEVEL_ENV_MAP.items():
        if env_var in env:
            _set_field(new_config, path, _cast_value(env[env_var], type_))

    # service 维度（按 service 别名前缀，大小写不敏感）
    name_to_idx = {s.name.upper(): i for i, s in enumerate(new_config.services)}
    for env_var, value in env.items():
        if not env_var.startswith("SSGC_"):
            continue
        # 跳过已处理的 detector / 顶层
        if env_var in _DETECTOR_ENV_MAP or env_var in _TOP_LEVEL_ENV_MAP:
            continue
        # SSGC_<NAME>_<FIELD>（NAME 是大写）
        for upper_name, idx in name_to_idx.items():
            prefix = f"SSGC_{upper_name}_"
            if env_var.startswith(prefix):
                suffix = env_var[len(prefix):]
                if suffix in _SERVICE_FIELD_BY_SUFFIX:
                    field, type_ = _SERVICE_FIELD_BY_SUFFIX[suffix]
                    setattr(
                        new_config.services[idx],
                        field,
                        _cast_value(value, type_),
                    )
                break

    return new_config


def apply_cli_overrides(config: AppConfig, **kwargs: Any) -> AppConfig:
    """应用 CLI 参数覆盖

    支持的字段（参见 `_CLI_FIELD_MAP`）：
    - `detector_url` / `api_key`
    - `report_interval` / `batch_size` / `max_queue_size`
    - `log_level`

    Args:
        config: 原始 AppConfig
        **kwargs: CLI 参数（None 值会被跳过）

    Raises:
        ValueError: 未知字段名
    """
    new_config = copy.deepcopy(config)

    for key, value in kwargs.items():
        if value is None:
            continue
        if key not in _CLI_FIELD_MAP:
            raise ValueError(
                f"unknown CLI override: {key!r} (allowed: {sorted(_CLI_FIELD_MAP)})"
            )
        path, type_ = _CLI_FIELD_MAP[key]
        _set_field(new_config, path, _cast_value(str(value), type_))

    return new_config


def validate_config(config: AppConfig) -> list[ConfigError]:
    """校验配置，返回所有错误（**不抛异常**）

    任何错误都映射到 CLI 退出码 1。
    """
    errors: list[ConfigError] = []

    # detector.url 是合法 HTTP(S) URL
    parsed = urlparse(config.detector.url)
    if parsed.scheme not in ("http", "https"):
        errors.append(
            ConfigError(
                code=ConfigErrorCode.CONFIG_VALIDATION_ERROR,
                field="detector.url",
                message=f"scheme must be http/https, got {parsed.scheme!r}",
            )
        )

    # detector.api_key 非空
    if not config.detector.api_key or not config.detector.api_key.strip():
        errors.append(
            ConfigError(
                code=ConfigErrorCode.CONFIG_MISSING_FIELD,
                field="detector.api_key",
                message="api_key is required (X-API-Key)",
            )
        )

    # detector.endpoint_path 必须以 / 开头（url 只含 scheme+host+port，路径放这里）
    ep = config.detector.endpoint_path
    if not ep or not ep.startswith("/"):
        errors.append(
            ConfigError(
                code=ConfigErrorCode.CONFIG_VALIDATION_ERROR,
                field="detector.endpoint_path",
                message=f"must start with '/', got {ep!r}",
            )
        )

    # 数值约束
    if config.detector.report_interval_sec <= 0:
        errors.append(
            ConfigError(
                code=ConfigErrorCode.CONFIG_VALIDATION_ERROR,
                field="detector.report_interval_sec",
                message="must be > 0",
            )
        )
    if config.detector.batch_size <= 0:
        errors.append(
            ConfigError(
                code=ConfigErrorCode.CONFIG_VALIDATION_ERROR,
                field="detector.batch_size",
                message="must be > 0",
            )
        )
    if config.detector.max_queue_size <= 0:
        errors.append(
            ConfigError(
                code=ConfigErrorCode.CONFIG_VALIDATION_ERROR,
                field="detector.max_queue_size",
                message="must be > 0",
            )
        )

    # services
    seen_ports: set[int] = set()
    for i, svc in enumerate(config.services):
        # port 范围 + 唯一性（0 表示自动分配，启动时由 OS 选定）
        if not (0 <= svc.port <= 65535):
            errors.append(
                ConfigError(
                    code=ConfigErrorCode.CONFIG_VALIDATION_ERROR,
                    field=f"services[{i}].port",
                    message=f"port must be in [0, 65535], got {svc.port}",
                )
            )
        if svc.port in seen_ports:
            errors.append(
                ConfigError(
                    code=ConfigErrorCode.CONFIG_VALIDATION_ERROR,
                    field=f"services[{i}].port",
                    message=f"port {svc.port} is duplicated",
                )
            )
        seen_ports.add(svc.port)

        # upstream URL
        parsed_up = urlparse(svc.upstream)
        if parsed_up.scheme not in ("http", "https"):
            errors.append(
                ConfigError(
                    code=ConfigErrorCode.CONFIG_VALIDATION_ERROR,
                    field=f"services[{i}].upstream",
                    message=f"scheme must be http/https, got {parsed_up.scheme!r}",
                )
            )

        # endpoint_type 枚举
        if svc.endpoint_type not in _VALID_ENDPOINT_TYPES:
            errors.append(
                ConfigError(
                    code=ConfigErrorCode.CONFIG_VALIDATION_ERROR,
                    field=f"services[{i}].endpoint_type",
                    message=f"must be one of {sorted(_VALID_ENDPOINT_TYPES)}, got {svc.endpoint_type!r}",
                )
            )

    return errors


def load_config_with_overrides(
    path: Path,
    **cli_overrides: Any,
) -> tuple[AppConfig, ConfigSources]:
    """三级加载：`config.json` → env → cli → `validate`

    Returns:
        (config, sources)：最终 AppConfig 与字段来源映射

    Raises:
        FileNotFoundError: config.json 不存在
        ValueError: JSON 损坏或字段缺失
        ConfigValidationError: 校验失败（含所有错误）
    """
    config = load_config_json(path)
    sources = ConfigSources()

    env_config = apply_env_overrides(config)
    env_changed = _diff_changed_paths(config, env_config)
    for p in env_changed:
        sources.sources[p] = ConfigSource.ENV

    final_config = apply_cli_overrides(env_config, **cli_overrides)
    cli_changed = _diff_changed_paths(env_config, final_config)
    for p in cli_changed:
        sources.sources[p] = ConfigSource.CLI

    # 收集生效的 env 变量名（用于 `config list` 输出）
    env = os.environ
    for env_var in env:
        if env_var.startswith("SSGC_"):
            sources.env_vars[env_var] = env[env_var]

    errors = validate_config(final_config)
    if errors:
        raise ConfigValidationError(errors)

    return final_config, sources
"""配置 schema + 校验 + 三级覆盖（详见 docs/design/saitec-safe-guard-cli-design.md §12.1）

⚠️ 骨架阶段：本文件仅定义接口签名，具体实现在 Phase B 落地。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .models import AppConfig, ConfigError, ConfigSources


def load_config_json(path: Path) -> AppConfig:
    """从 `config.json` 加载完整 AppConfig（不应用 env / cli 覆盖）"""
    raise NotImplementedError("Phase B 实现")


def apply_env_overrides(config: AppConfig) -> AppConfig:
    """应用环境变量覆盖（`SAITEC_*` 前缀，详见 design.md §12.1）"""
    raise NotImplementedError("Phase B 实现")


def apply_cli_overrides(config: AppConfig, **kwargs: Any) -> AppConfig:
    """应用命令行参数覆盖（优先级最高）"""
    raise NotImplementedError("Phase B 实现")


def validate_config(config: AppConfig) -> list[ConfigError]:
    """校验配置，返回所有错误（不抛异常）

    任何错误都映射到 CLI 退出码 1。
    """
    raise NotImplementedError("Phase B 实现")


def load_config_with_overrides(
    path: Path,
    **cli_overrides: Any,
) -> tuple[AppConfig, ConfigSources]:
    """三级加载：`config.json` → env → cli → `validate`

    顺序：后两步覆盖前一步。返回 (`AppConfig`, `ConfigSources`) 用于 `config list`。
    """
    raise NotImplementedError("Phase B 实现")
"""路径解析（与 docs/design/saitec-safe-guard-cli-design.md §16 一致）

仅依赖 `platformdirs`，无任何 IO。
"""
from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_data_dir

APP_NAME = "saitec"


def resolve_config_dir() -> Path:
    """解析配置目录：
    - `$SAITEC_CONFIG` 显式指定时，取其父目录
    - 否则走 `platformdirs` 跨平台默认（Linux/macOS/Windows 用户目录）
    """
    explicit = os.environ.get("SAITEC_CONFIG")
    if explicit:
        return Path(explicit).expanduser().resolve().parent
    return Path(user_data_dir(APP_NAME, appauthor=False))


def resolve_config_path() -> Path:
    """`config.json` 完整路径"""
    return resolve_config_dir() / "config.json"


def resolve_data_dir() -> Path:
    """数据目录（records / db / logs），跟随 config_dir"""
    return resolve_config_dir()


def ensure_dirs() -> None:
    """首次 init 时调用，创建子目录，权限 0o700"""
    d = resolve_config_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    (d / "records").mkdir(exist_ok=True, mode=0o700)
    (d / "logs").mkdir(exist_ok=True, mode=0o700)
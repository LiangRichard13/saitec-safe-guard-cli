"""路径解析

仅标准库，无任何 IO。数据根目录为 `~/.ssgc`（品牌统一 SSGC）。
"""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "ssgc"


def resolve_config_dir() -> Path:
    """解析配置目录：
    - `$SSGC_CONFIG` 显式指定时，取其父目录
    - 否则用 `~/.ssgc`（跨平台统一 home 下的点目录）
    """
    explicit = os.environ.get("SSGC_CONFIG")
    if explicit:
        return Path(explicit).expanduser().resolve().parent
    return Path.home() / f".{APP_NAME}"


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

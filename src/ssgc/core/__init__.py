"""core — 最底层（Layer 1）

无 IO、无 aiohttp/typer/sqlite3 依赖，仅标准库。

可单独 `pip install` 后被任意脚本 / 测试 import，是整套系统的"领域字典"。
"""
from .config import (
    apply_cli_overrides,
    apply_env_overrides,
    load_config_json,
    load_config_with_overrides,
    validate_config,
)
from .models import (
    AppConfig,
    ConfigError,
    ConfigErrorCode,
    ConfigSource,
    ConfigSources,
    ConfigValidationError,
    DetectionResult,
    DetectorConfig,
    EndpointSpec,
    Record,
    ReportCursor,
)
from .paths import (
    APP_NAME,
    ensure_dirs,
    resolve_config_dir,
    resolve_config_path,
    resolve_data_dir,
)
from .utils import now_iso8601, redact_headers

__all__ = [
    # models
    "Record",
    "DetectionResult",
    "EndpointSpec",
    "DetectorConfig",
    "AppConfig",
    "ReportCursor",
    "ConfigErrorCode",
    "ConfigError",
    "ConfigSource",
    "ConfigSources",
    "ConfigValidationError",
    # config
    "load_config_json",
    "apply_env_overrides",
    "apply_cli_overrides",
    "validate_config",
    "load_config_with_overrides",
    # paths
    "APP_NAME",
    "resolve_config_dir",
    "resolve_config_path",
    "resolve_data_dir",
    "ensure_dirs",
    # utils
    "now_iso8601",
    "redact_headers",
]
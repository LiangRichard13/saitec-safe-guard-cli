"""数据模型（与 docs/design/saitec-safe-guard-cli-design.md §6 一致）

最底层（Layer 1），纯数据类，无任何 IO。可单独 import。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


@dataclass
class Record:
    """归一化记录，上报与存储的统一格式"""

    record_id: str
    service: str
    endpoint_type: str
    upstream: str
    path: str
    timestamp: str
    elapsed_ms: int
    status_code: int
    error: str | None
    request: dict[str, Any]
    response: dict[str, Any]


@dataclass
class DetectionResult:
    """检测结果，存储到 SQLite 的 `detection_results` 表

    由 runtime 把 `Record`（来自 proxy/recorder）+ 检测服务器响应合并而成。
    """

    # 来自 Record（用于 SQL 关联与查询）
    record_id: str
    service: str
    endpoint_type: str
    upstream: str
    timestamp: str
    status_code: int
    elapsed_ms: int

    # 来自 Record（可选）
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = None
    error: str | None = None

    # 来自检测服务器
    detection_status: str = "clean"  # clean / suspicious / violation / error
    risk_level: str | None = None  # low / medium / high / critical
    detection_detail: dict[str, Any] | None = None  # 完整检测响应
    detected_at: str = ""  # ISO8601，检测服务器返回时间


@dataclass
class EndpointSpec:
    """单个反向代理服务的配置"""

    name: str
    port: int
    upstream: str
    endpoint_type: str
    record_body: bool = True


@dataclass
class DetectorConfig:
    """检测服务器配置"""

    url: str
    api_key: str
    report_interval_sec: int = 60
    batch_size: int = 100
    max_queue_size: int = 10000


@dataclass
class AppConfig:
    """完整应用配置 = DetectorConfig + List[EndpointSpec]"""

    detector: DetectorConfig
    services: list[EndpointSpec]
    log_level: str = "INFO"


@dataclass
class ReportCursor:
    """上报游标，用于断点续传"""

    last_record_id: str | None = None
    last_timestamp: str | None = None
    updated_at: str = "1970-01-01T00:00:00Z"


class ConfigErrorCode(str, Enum):
    """配置错误类别（对应 CLI 退出码 1）"""

    CONFIG_PARSE_ERROR = "CONFIG_PARSE_ERROR"
    CONFIG_VALIDATION_ERROR = "CONFIG_VALIDATION_ERROR"
    CONFIG_MISSING_FIELD = "CONFIG_MISSING_FIELD"


@dataclass
class ConfigError:
    """配置错误详情"""

    code: ConfigErrorCode
    field: str
    message: str


class ConfigSource(str, Enum):
    """字段来源（供 config list 输出）"""

    CONFIG = "config"
    ENV = "env"
    CLI = "cli"
    DEFAULT = "default"


@dataclass
class ConfigSources:
    """记录每个字段的来源（用于 config list 命令）"""

    sources: dict[str, ConfigSource] = field(default_factory=dict)
    env_vars: dict[str, str] = field(default_factory=dict)


class ConfigValidationError(Exception):
    """配置校验失败（包含错误列表）"""

    def __init__(self, errors: list[ConfigError]) -> None:
        super().__init__(f"config validation failed: {len(errors)} error(s)")
        self.errors = errors
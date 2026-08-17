"""core 数据模型 + 工具测试"""
from __future__ import annotations

from saitec.core.models import (
    AppConfig,
    ConfigError,
    ConfigErrorCode,
    ConfigSources,
    DetectionResult,
    DetectorConfig,
    EndpointSpec,
    Record,
    ReportCursor,
)
from saitec.core.paths import (
    APP_NAME,
    ensure_dirs,
    resolve_config_dir,
    resolve_config_path,
    resolve_data_dir,
)
from saitec.core.utils import now_iso8601, redact_headers


def test_record_minimal() -> None:
    """Record dataclass 可以用全部字段构造"""
    rec = Record(
        record_id="550e8400-e29b-41d4-a716-446655440000",
        service="openai-chat-completions",
        endpoint_type="openai-chat-completions",
        upstream="https://api.openai.com",
        path="/v1/chat/completions",
        timestamp="2026-08-14T12:00:00Z",
        elapsed_ms=812,
        status_code=200,
        error=None,
        request={"model": "gpt-4o"},
        response={"content": "hi"},
    )
    assert rec.record_id == "550e8400-e29b-41d4-a716-446655440000"
    assert rec.elapsed_ms == 812
    assert rec.error is None


def test_endpoint_spec_defaults() -> None:
    """EndpointSpec 默认值"""
    spec = EndpointSpec(
        name="openai-chat-completions",
        port=9001,
        upstream="https://api.openai.com",
        endpoint_type="openai-chat-completions",
    )
    assert spec.record_body is True  # 默认记录 body


def test_detector_config_defaults() -> None:
    """DetectorConfig 默认值"""
    cfg = DetectorConfig(
        url="http://detector:8080",
        api_key="sk-test",
    )
    assert cfg.report_interval_sec == 60
    assert cfg.batch_size == 100
    assert cfg.max_queue_size == 10000


def test_app_config_composition() -> None:
    """AppConfig 包含 detector + services"""
    cfg = AppConfig(
        detector=DetectorConfig(url="http://d", api_key="k"),
        services=[
            EndpointSpec(
                name="oai", port=9001,
                upstream="https://api.openai.com",
                endpoint_type="openai-chat-completions",
            ),
        ],
    )
    assert len(cfg.services) == 1
    assert cfg.log_level == "INFO"


def test_report_cursor_defaults() -> None:
    """ReportCursor 默认游标"""
    c = ReportCursor()
    assert c.last_record_id is None
    assert c.last_timestamp is None
    assert c.updated_at == "1970-01-01T00:00:00Z"


def test_config_error_code_values() -> None:
    """ConfigErrorCode 三类枚举值"""
    assert ConfigErrorCode.CONFIG_PARSE_ERROR == "CONFIG_PARSE_ERROR"
    assert ConfigErrorCode.CONFIG_VALIDATION_ERROR == "CONFIG_VALIDATION_ERROR"
    assert ConfigErrorCode.CONFIG_MISSING_FIELD == "CONFIG_MISSING_FIELD"


def test_config_sources_empty() -> None:
    """ConfigSources 默认空"""
    s = ConfigSources()
    assert s.sources == {}
    assert s.env_vars == {}


def test_paths_app_name() -> None:
    """APP_NAME 是 'saitec'"""
    assert APP_NAME == "saitec"


def test_paths_resolve_no_crash() -> None:
    """路径解析函数不应抛异常（具体路径随平台/环境变化）"""
    # 不验证具体值，只验证不抛
    _ = resolve_config_dir()
    _ = resolve_config_path()
    _ = resolve_data_dir()


def test_now_iso8601_format() -> None:
    """now_iso8601 返回 ISO8601 字符串"""
    s = now_iso8601()
    # 形如 2026-08-14T12:34:56.789+00:00
    assert "T" in s
    assert s.endswith("+00:00") or s.endswith("Z")


def test_redact_headers_strips_sensitive() -> None:
    """redact_headers 屏蔽 Authorization / X-API-Key 等敏感头"""
    h = {
        "Authorization": "Bearer sk-real-key",
        "X-API-Key": "sk-real-key",
        "Content-Type": "application/json",
    }
    out = redact_headers(h)
    assert out["Authorization"] == "***"
    assert out["X-API-Key"] == "***"
    assert out["Content-Type"] == "application/json"


def test_redact_headers_case_insensitive() -> None:
    """redact_headers 大小写不敏感"""
    out = redact_headers({"authorization": "Bearer x"})
    assert out["authorization"] == "***"


def test_redact_headers_empty() -> None:
    """redact_headers 空 dict 返回空"""
    assert redact_headers({}) == {}
    assert redact_headers(None) == {}  # type: ignore[arg-type]


def test_detection_result_construction() -> None:
    """DetectionResult 构造（含 Record 字段 + 检测字段）"""
    r = DetectionResult(
        record_id="abc",
        service="svc",
        endpoint_type="openai-chat-completions",
        upstream="https://api.openai.com",
        timestamp="2026-08-14T12:00:00Z",
        status_code=200,
        elapsed_ms=812,
        detection_status="clean",
        risk_level="low",
        detection_detail=None,
        detected_at="2026-08-14T12:00:01Z",
    )
    assert r.detection_status == "clean"
    assert r.elapsed_ms == 812


def test_config_error_construction() -> None:
    """ConfigError 构造"""
    e = ConfigError(
        code=ConfigErrorCode.CONFIG_MISSING_FIELD,
        field="detector.api_key",
        message="api_key is empty",
    )
    assert e.code == ConfigErrorCode.CONFIG_MISSING_FIELD
    assert e.field == "detector.api_key"
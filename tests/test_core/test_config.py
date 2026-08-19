"""core/config.py 单元测试"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from saitec.core.config import (
    apply_cli_overrides,
    apply_env_overrides,
    load_config_json,
    load_config_with_overrides,
    validate_config,
)
from saitec.core.models import (
    AppConfig,
    ConfigErrorCode,
    ConfigSource,
    ConfigValidationError,
    DetectorConfig,
    EndpointSpec,
)


# ============================================================
# Fixture：清空 SAITEC_* 环境变量，避免测试互相污染
# ============================================================


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个测试前清空 SAITEC_* 环境变量"""
    for k in list(os.environ):
        if k.startswith("SAITEC_"):
            monkeypatch.delenv(k)


@pytest.fixture
def valid_config() -> AppConfig:
    """一份合法配置（默认各字段合法）"""
    return AppConfig(
        detector=DetectorConfig(
            url="http://detector:8080",
            api_key="sk-test",
        ),
        services=[
            EndpointSpec(
                name="openai-chat-completions",
                port=9001,
                upstream="https://api.openai.com",
                endpoint_type="openai-chat-completions",
            ),
            EndpointSpec(
                name="anthropic-messages",
                port=9002,
                upstream="https://api.anthropic.com",
                endpoint_type="anthropic-messages",
            ),
        ],
    )


@pytest.fixture
def valid_config_path(tmp_path: Path, valid_config: AppConfig) -> Path:
    """写到 tmp_path，返回路径"""
    path = tmp_path / "config.json"
    data = {
        "detector": {
            "url": valid_config.detector.url,
            "api_key": valid_config.detector.api_key,
        },
        "services": [
            {
                "name": s.name,
                "port": s.port,
                "upstream": s.upstream,
                "endpoint_type": s.endpoint_type,
                "record_body": s.record_body,
            }
            for s in valid_config.services
        ],
        "log_level": valid_config.log_level,
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ============================================================
# load_config_json
# ============================================================


def test_load_config_json_ok(valid_config_path: Path) -> None:
    cfg = load_config_json(valid_config_path)
    assert cfg.detector.url == "http://detector:8080"
    assert cfg.detector.api_key == "sk-test"
    assert len(cfg.services) == 2
    assert cfg.services[0].name == "openai-chat-completions"
    assert cfg.log_level == "INFO"


def test_load_config_json_with_defaults(tmp_path: Path) -> None:
    """最小配置：detector 只要 url/api_key，其他用默认"""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "detector": {"url": "http://d", "api_key": "k"},
                "services": [
                    {
                        "name": "s",
                        "port": 9001,
                        "upstream": "https://api.example.com",
                        "endpoint_type": "openai-chat-completions",
                    }
                ],
            }
        )
    )
    cfg = load_config_json(path)
    assert cfg.detector.report_interval_sec == 60
    assert cfg.services[0].record_body is True


def test_load_config_json_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_config_json(tmp_path / "nope.json")


def test_load_config_json_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("not json {")
    with pytest.raises(ValueError, match="invalid JSON"):
        load_config_json(path)


def test_load_config_json_missing_detector(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"services": []}))
    with pytest.raises(KeyError):
        load_config_json(path)


def test_load_config_json_missing_services_key(tmp_path: Path) -> None:
    """services 缺失应该默认为空列表（允许纯 detector 配置）"""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"detector": {"url": "http://d", "api_key": "k"}})
    )
    cfg = load_config_json(path)
    assert cfg.services == []


# ============================================================
# apply_env_overrides
# ============================================================


def test_apply_env_overrides_detector_url(
    valid_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAITEC_DETECTOR_URL", "http://override:9090")
    new = apply_env_overrides(valid_config)
    assert new.detector.url == "http://override:9090"


def test_apply_env_overrides_report_interval_int(
    valid_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAITEC_REPORT_INTERVAL", "120")
    new = apply_env_overrides(valid_config)
    assert new.detector.report_interval_sec == 120
    assert isinstance(new.detector.report_interval_sec, int)


def test_apply_env_overrides_log_level(
    valid_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAITEC_LOG_LEVEL", "DEBUG")
    new = apply_env_overrides(valid_config)
    assert new.log_level == "DEBUG"


def test_apply_env_overrides_service_port(
    valid_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAITEC_OPENAI-CHAT-COMPLETIONS_PORT", "9999")
    new = apply_env_overrides(valid_config)
    assert new.services[0].port == 9999
    assert new.services[1].port == 9002  # 未被覆盖


def test_apply_env_overrides_service_record_body(
    valid_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAITEC_OPENAI-CHAT-COMPLETIONS_RECORD_BODY", "false")
    new = apply_env_overrides(valid_config)
    assert new.services[0].record_body is False


def test_apply_env_overrides_unknown_service_ignored(
    valid_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SAITEC_UNKNOWN_PORT 没有匹配的 service 名，应被忽略"""
    monkeypatch.setenv("SAITEC_UNKNOWN_PORT", "9999")
    new = apply_env_overrides(valid_config)
    assert new.services[0].port == 9001


def test_apply_env_overrides_no_changes(
    valid_config: AppConfig,
) -> None:
    """无 SAITEC_* 环境变量时，配置应原样"""
    new = apply_env_overrides(valid_config)
    assert new == valid_config


# ============================================================
# apply_cli_overrides
# ============================================================


def test_apply_cli_overrides_detector_url(valid_config: AppConfig) -> None:
    new = apply_cli_overrides(valid_config, detector_url="http://cli:8888")
    assert new.detector.url == "http://cli:8888"


def test_apply_cli_overrides_report_interval_int(valid_config: AppConfig) -> None:
    new = apply_cli_overrides(valid_config, report_interval=45)
    assert new.detector.report_interval_sec == 45


def test_apply_cli_overrides_none_skipped(valid_config: AppConfig) -> None:
    """None 值（typer 未指定）应被跳过"""
    new = apply_cli_overrides(valid_config, detector_url=None)
    assert new.detector.url == valid_config.detector.url


def test_apply_cli_overrides_unknown_field(valid_config: AppConfig) -> None:
    with pytest.raises(ValueError, match="unknown CLI override"):
        apply_cli_overrides(valid_config, unknown_field="x")


# ============================================================
# validate_config
# ============================================================


def test_validate_config_valid(valid_config: AppConfig) -> None:
    errors = validate_config(valid_config)
    assert errors == []


def test_validate_config_empty_api_key() -> None:
    cfg = AppConfig(
        detector=DetectorConfig(url="http://d", api_key=""),
        services=[
            EndpointSpec(
                name="s", port=9001,
                upstream="https://api.example.com",
                endpoint_type="openai-chat-completions",
            )
        ],
    )
    errors = validate_config(cfg)
    assert any(e.code == ConfigErrorCode.CONFIG_MISSING_FIELD for e in errors)
    assert any(e.field == "detector.api_key" for e in errors)


def test_validate_config_invalid_url_scheme() -> None:
    cfg = AppConfig(
        detector=DetectorConfig(url="ftp://detector", api_key="k"),
        services=[
            EndpointSpec(
                name="s", port=9001,
                upstream="https://api.example.com",
                endpoint_type="openai-chat-completions",
            )
        ],
    )
    errors = validate_config(cfg)
    assert any(
        e.field == "detector.url" and "scheme" in e.message
        for e in errors
    )


def test_validate_config_duplicate_port() -> None:
    cfg = AppConfig(
        detector=DetectorConfig(url="http://d", api_key="k"),
        services=[
            EndpointSpec(
                name="a", port=9001,
                upstream="https://api.example.com",
                endpoint_type="openai-chat-completions",
            ),
            EndpointSpec(
                name="b", port=9001,
                upstream="https://api.example.com",
                endpoint_type="openai-chat-completions",
            ),
        ],
    )
    errors = validate_config(cfg)
    assert any("duplicated" in e.message for e in errors)


def test_validate_config_port_out_of_range() -> None:
    cfg = AppConfig(
        detector=DetectorConfig(url="http://d", api_key="k"),
        services=[
            EndpointSpec(
                name="s", port=99999,
                upstream="https://api.example.com",
                endpoint_type="openai-chat-completions",
            )
        ],
    )
    errors = validate_config(cfg)
    assert any("[0, 65535]" in e.message for e in errors)


def test_validate_config_invalid_endpoint_type() -> None:
    cfg = AppConfig(
        detector=DetectorConfig(url="http://d", api_key="k"),
        services=[
            EndpointSpec(
                name="s", port=9001,
                upstream="https://api.example.com",
                endpoint_type="totally-bogus",
            )
        ],
    )
    errors = validate_config(cfg)
    assert any(
        e.field.startswith("services[0].endpoint_type")
        for e in errors
    )


def test_validate_config_multiple_errors() -> None:
    """多个错误都应被报告，不在第一个错误就停"""
    cfg = AppConfig(
        detector=DetectorConfig(url="ftp://d", api_key=""),
        services=[
            EndpointSpec(
                name="s", port=9001,
                upstream="not-a-url",
                endpoint_type="bogus",
            )
        ],
    )
    errors = validate_config(cfg)
    assert len(errors) >= 4


# ============================================================
# load_config_with_overrides（综合）
# ============================================================


def test_load_config_with_overrides_basic(
    valid_config_path: Path,
) -> None:
    cfg, sources = load_config_with_overrides(valid_config_path)
    assert cfg.detector.url == "http://detector:8080"
    assert sources.sources == {}  # 无覆盖


def test_load_config_with_overrides_cli_wins(
    valid_config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAITEC_DETECTOR_URL", "http://env:1234")
    cfg, sources = load_config_with_overrides(
        valid_config_path, detector_url="http://cli:5678"
    )
    # CLI 优先级最高
    assert cfg.detector.url == "http://cli:5678"
    # 来源：CLI（而非 ENV，因为 CLI 覆盖了同一字段）
    assert sources.sources.get("detector.url") == ConfigSource.CLI


def test_load_config_with_overrides_env_source(
    valid_config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAITEC_LOG_LEVEL", "DEBUG")
    cfg, sources = load_config_with_overrides(valid_config_path)
    assert cfg.log_level == "DEBUG"
    assert sources.sources.get("log_level") == ConfigSource.ENV


def test_load_config_with_overrides_validation_error(
    tmp_path: Path,
) -> None:
    """config 缺少 api_key 应抛 ConfigValidationError"""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "detector": {"url": "http://d", "api_key": ""},
                "services": [],
            }
        )
    )
    with pytest.raises(ConfigValidationError) as exc_info:
        load_config_with_overrides(path)
    assert len(exc_info.value.errors) >= 1
    assert any(
        e.field == "detector.api_key" for e in exc_info.value.errors
    )


def test_load_config_with_overrides_priority(
    valid_config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """config < env < cli 的优先级"""
    monkeypatch.setenv("SAITEC_REPORT_INTERVAL", "120")
    cfg, _ = load_config_with_overrides(
        valid_config_path, report_interval=300
    )
    # cli 覆盖 env
    assert cfg.detector.report_interval_sec == 300
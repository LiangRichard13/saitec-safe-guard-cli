"""CLI 命令测试（用 typer.testing.CliRunner）"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from saitec.cli.main import app

runner = CliRunner()


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 SAITEC_CONFIG 指到临时目录，保证 init/validate 用临时 config"""
    cfg_path = tmp_path / "config.json"
    monkeypatch.setenv("SAITEC_CONFIG", str(cfg_path))
    monkeypatch.delenv("SAITEC_API_KEY", raising=False)
    monkeypatch.delenv("SAITEC_DETECTOR_URL", raising=False)
    return tmp_path


# ============================================================
# init
# ============================================================


def test_init_noninteractive_creates_config(isolated: Path) -> None:
    result = runner.invoke(
        app, ["init", "--api-key", "sk-test", "--detector-url", "http://detector:8080"]
    )
    assert result.exit_code == 0
    cfg = json.loads((isolated / "config.json").read_text())
    assert cfg["detector"]["url"] == "http://detector:8080"
    assert cfg["detector"]["api_key"] == "sk-test"
    assert len(cfg["services"]) == 3  # 默认 3 个服务


def test_init_refuses_overwrite_without_force(isolated: Path) -> None:
    (isolated / "config.json").write_text("{}")
    result = runner.invoke(app, ["init", "--api-key", "x", "--detector-url", "http://d"])
    assert "已存在" in result.stderr
    assert (isolated / "config.json").read_text() == "{}"  # 未覆盖


def test_init_force_overwrites(isolated: Path) -> None:
    (isolated / "config.json").write_text("{}")
    result = runner.invoke(
        app, ["init", "--api-key", "x", "--detector-url", "http://d", "--force"]
    )
    assert result.exit_code == 0
    cfg = json.loads((isolated / "config.json").read_text())
    assert cfg["detector"]["api_key"] == "x"


def test_init_missing_api_key(isolated: Path) -> None:
    result = runner.invoke(
        app, ["init", "--detector-url", "http://detector:8080"],
        input="\n",  # stdin 非 TTY → _prompt 返回默认，api_key 空
    )
    assert result.exit_code == 1  # P1-10：用户错误 → exit 1
    assert "缺少 api_key" in result.stderr


# ============================================================
# validate
# ============================================================


def test_validate_ok(isolated: Path) -> None:
    runner.invoke(app, ["init", "--api-key", "sk", "--detector-url", "http://d:8080"])
    result = runner.invoke(app, ["validate"])
    assert result.exit_code == 0
    assert "valid" in result.stdout


def test_validate_missing_config(isolated: Path) -> None:
    result = runner.invoke(app, ["validate"])
    assert result.exit_code == 1
    assert "不存在" in result.stderr


def test_validate_invalid_config(isolated: Path) -> None:
    (isolated / "config.json").write_text(
        json.dumps({"detector": {"url": "ftp://d", "api_key": ""}, "services": []})
    )
    result = runner.invoke(app, ["validate"])
    assert result.exit_code == 1
    assert "校验失败" in result.stderr


# ============================================================
# config get / set / unset / list
# ============================================================


@pytest.fixture
def ready_config(isolated: Path) -> Path:
    runner.invoke(app, ["init", "--api-key", "sk", "--detector-url", "http://d:8080"])
    return isolated


def test_config_get(ready_config: Path) -> None:
    result = runner.invoke(app, ["config", "get", "detector.url"])
    assert result.exit_code == 0
    assert "http://d:8080" in result.stdout


def test_config_get_json(ready_config: Path) -> None:
    result = runner.invoke(app, ["config", "get", "detector.url", "--json"])
    data = json.loads(result.stdout)
    assert data["ok"] is True
    assert data["data"]["value"] == "http://d:8080"


def test_config_get_not_found(ready_config: Path) -> None:
    result = runner.invoke(app, ["config", "get", "detector.nope"])
    assert result.exit_code == 1
    assert "不存在" in result.stderr


def test_config_set_persists(ready_config: Path) -> None:
    result = runner.invoke(app, ["config", "set", "detector.report_interval_sec", "120"])
    assert result.exit_code == 0
    cfg = json.loads((ready_config / "config.json").read_text())
    assert cfg["detector"]["report_interval_sec"] == 120


def test_config_set_invalid_type(ready_config: Path) -> None:
    """设 int 字段为非法值 → 报错且不写入"""
    result = runner.invoke(app, ["config", "set", "detector.batch_size", "not-int"])
    assert result.exit_code == 1
    cfg = json.loads((ready_config / "config.json").read_text())
    assert cfg["detector"]["batch_size"] == 500  # 默认值（P0-8 后从 100 改为 500）


def test_config_set_invalid_value_rollback(ready_config: Path) -> None:
    """设置导致校验失败 → 不写入"""
    result = runner.invoke(app, ["config", "set", "detector.api_key", ""])
    assert result.exit_code == 1
    cfg = json.loads((ready_config / "config.json").read_text())
    assert cfg["detector"]["api_key"] == "sk"  # 未变


def test_config_unset(ready_config: Path) -> None:
    result = runner.invoke(app, ["config", "unset", "log_level"])
    assert result.exit_code == 0
    cfg = json.loads((ready_config / "config.json").read_text())
    assert "log_level" not in cfg


def test_config_list(ready_config: Path) -> None:
    result = runner.invoke(app, ["config", "list", "--json"])
    data = json.loads(result.stdout)
    assert data["ok"] is True
    assert "detector.url" in data["data"]["config"]
    assert "services.openai-chat-completions.port" in data["data"]["config"]


def test_config_backup_created_on_set(ready_config: Path) -> None:
    runner.invoke(app, ["config", "set", "log_level", "DEBUG"])
    backups = list(ready_config.glob("config.json.bak.*"))
    assert len(backups) == 1


def test_config_get_redacts_api_key(ready_config: Path) -> None:
    """api_key 明文不应出现在 config get 输出（P0-6）"""
    result = runner.invoke(app, ["config", "get", "detector.api_key", "--json"])
    data = json.loads(result.stdout)
    assert data["ok"] is True
    value = data["data"]["value"]
    assert "***" in value  # 脱敏标记
    # 完整明文不得出现在任何输出
    full_key = json.loads((ready_config / "config.json").read_text())["detector"]["api_key"]
    assert full_key not in result.stdout


def test_config_list_redacts_api_key(ready_config: Path) -> None:
    """api_key 明文不应出现在 config list 输出（P0-6）"""
    result = runner.invoke(app, ["config", "list", "--json"])
    data = json.loads(result.stdout)
    api_key_val = data["data"]["config"]["detector.api_key"]["value"]
    assert "***" in api_key_val  # 脱敏标记
    full_key = "sk"  # 测试 fixture 用的明文
    assert full_key not in result.stdout  # 完整明文不得出现


# ============================================================
# status
# ============================================================


def test_status_not_running(ready_config: Path) -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert '"running": false' in result.stdout or "running" in result.stdout


def test_status_json_shape(ready_config: Path) -> None:
    result = runner.invoke(app, ["status", "--json"])
    data = json.loads(result.stdout)
    assert data["ok"] is True
    assert "services" in data["data"]
    assert len(data["data"]["services"]) == 3


# ============================================================
# doctor
# ============================================================


def test_doctor_quick(ready_config: Path) -> None:
    result = runner.invoke(app, ["doctor", "--quick", "--json"])
    data = json.loads(result.stdout)
    assert data["ok"] is True
    assert "checks" in data["data"]


def test_doctor_missing_config(isolated: Path) -> None:
    result = runner.invoke(app, ["doctor", "--quick"])
    assert result.exit_code == 1  # P1-10
    assert "不存在" in result.stderr


# ============================================================
# report（无库时）
# ============================================================


def test_report_no_db(ready_config: Path) -> None:
    result = runner.invoke(app, ["report"])
    assert result.exit_code == 1  # P1-10
    assert "不存在" in result.stderr


# ============================================================
# purge（空目录）
# ============================================================


def test_purge_empty(ready_config: Path) -> None:
    result = runner.invoke(app, ["purge", "--dry-run", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["data"]["removed_jsonl_files"] == []
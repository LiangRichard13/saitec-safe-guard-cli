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
        app, ["init", "--api-key", "sk-test-12345678", "--detector-url", "http://detector:8080", "--upstream", "http://127.0.0.1:9999"]
    )
    assert result.exit_code == 0
    cfg = json.loads((isolated / "config.json").read_text())
    assert cfg["detector"]["url"] == "http://detector:8080"
    assert cfg["detector"]["api_key"] == "sk-test-12345678"
    assert len(cfg["services"]) == 1  # init 单服务（多端点用 service add）


def test_init_refuses_overwrite_without_force(isolated: Path) -> None:
    (isolated / "config.json").write_text("{}")
    result = runner.invoke(app, ["init", "--api-key", "sk-test-12345678", "--detector-url", "http://d", "--upstream", "http://127.0.0.1:9999"])
    assert "已存在" in result.stderr
    assert (isolated / "config.json").read_text() == "{}"  # 未覆盖


def test_init_force_overwrites(isolated: Path) -> None:
    (isolated / "config.json").write_text("{}")
    result = runner.invoke(
        app, ["init", "--api-key", "sk-test-12345678", "--detector-url", "http://d", "--upstream", "http://127.0.0.1:9999", "--force"]
    )
    assert result.exit_code == 0
    cfg = json.loads((isolated / "config.json").read_text())
    assert cfg["detector"]["api_key"] == "sk-test-12345678"


def test_init_missing_api_key(isolated: Path) -> None:
    result = runner.invoke(
        app, ["init", "--detector-url", "http://detector:8080", "--upstream", "http://127.0.0.1:9999"],
        input="\n",  # stdin 非 TTY → _prompt 返回默认，api_key 空
    )
    assert result.exit_code == 1  # P1-10：用户错误 → exit 1
    assert "缺少 api_key" in result.stderr


# ============================================================
# validate
# ============================================================


def test_validate_ok(isolated: Path) -> None:
    runner.invoke(app, ["init", "--api-key", "sk-test-12345678", "--detector-url", "http://d:8080", "--upstream", "http://127.0.0.1:9999"])
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
    runner.invoke(app, ["init", "--api-key", "sk-test-12345678", "--detector-url", "http://d:8080", "--upstream", "http://127.0.0.1:9999"])
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
    assert cfg["detector"]["api_key"] == "sk-test-12345678"  # 未变


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
    full_key = "sk-test-12345678"  # 测试 fixture 用的明文
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
    assert len(data["data"]["services"]) == 1


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


# ============================================================
# report（有数据时）
# ============================================================


def test_report_with_data(ready_config: Path) -> None:
    """有 SQLite 数据时 report 应正常返回"""
    import asyncio
    from datetime import datetime, timezone
    from saitec.store.store import Store
    from saitec.core.models import DetectionResult

    db_path = ready_config / "results.db"
    # 用 Store 初始化表结构
    async def setup():
        store = Store(db_path)
        await store.save_results([
            DetectionResult(
                record_id="r1",
                service="svc-a",
                endpoint_type="openai-chat-completions",
                upstream="http://upstream",
                status_code=200,
                timestamp=datetime.now(timezone.utc).isoformat(),
                detection_status="clean",
                risk_level="low",
                detection_detail={},
                detected_at=datetime.now(timezone.utc).isoformat(),
                model="gpt-4o",
                elapsed_ms=100,
            )
        ])
    asyncio.run(setup())

    result = runner.invoke(app, ["report", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["ok"] is True
    assert data["data"]["count"] == 1
    assert data["data"]["results"][0]["record_id"] == "r1"


def test_report_since_filter(ready_config: Path) -> None:
    """--since 过滤应生效"""
    import asyncio
    from datetime import datetime, timedelta, timezone
    from saitec.store.store import Store
    from saitec.core.models import DetectionResult

    db_path = ready_config / "results.db"
    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=2)

    async def setup():
        store = Store(db_path)
        await store.save_results([
            DetectionResult(
                record_id="r_old",
                service="svc-a",
                endpoint_type="openai-chat-completions",
                upstream="http://upstream",
                status_code=200,
                timestamp=old.isoformat(),
                detection_status="clean",
                risk_level="low",
                detection_detail={},
                detected_at=old.isoformat(),
                model="gpt-4o",
                elapsed_ms=100,
            ),
            DetectionResult(
                record_id="r_new",
                service="svc-a",
                endpoint_type="openai-chat-completions",
                upstream="http://upstream",
                status_code=200,
                timestamp=now.isoformat(),
                detection_status="clean",
                risk_level="low",
                detection_detail={},
                detected_at=now.isoformat(),
                model="gpt-4o",
                elapsed_ms=100,
            )
        ])
    asyncio.run(setup())

    result = runner.invoke(app, ["report", "--since", "30m", "--json"])
    data = json.loads(result.stdout)
    assert data["ok"] is True
    assert data["data"]["count"] == 1
    assert data["data"]["results"][0]["record_id"] == "r_new"


# ============================================================
# redo
# ============================================================


def test_redo_record_not_found(ready_config: Path) -> None:
    """redo 找不到 record_id 应报错"""
    result = runner.invoke(app, ["redo", "nonexistent-id", "--json"])
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert data["ok"] is False
    assert "未找到" in data["error"]["message"]


def test_redo_success(ready_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """redo 找到记录并重报应成功"""
    import json as json_lib
    from unittest.mock import AsyncMock, MagicMock

    # 准备 JSONL
    records_dir = ready_config / "records"
    records_dir.mkdir(parents=True, exist_ok=True)
    jsonl = records_dir / "records-2026-08-20.jsonl"
    jsonl.write_text(
        json_lib.dumps(
            {
                "record_id": "r123",
                "service": "svc-a",
                "endpoint_type": "openai-chat-completions",
                "upstream": "http://up",
                "path": "/v1/chat/completions",
                "timestamp": "2026-08-20T10:00:00Z",
                "elapsed_ms": 100,
                "status_code": 200,
                "request": {},
                "response": {},
            }
        )
        + "\n"
    )

    # mock reporter + store
    from saitec.cli.commands.redo import _run
    from unittest.mock import patch

    mock_result = {
        "record_id": "r123",
        "reported": True,
        "detection_status": "clean",
        "risk_level": "low",
    }

    with patch("saitec.cli.commands.redo._run", return_value=mock_result):
        result = runner.invoke(app, ["redo", "r123", "--json"])

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["ok"] is True
    assert data["data"]["record_id"] == "r123"


# ============================================================
# purge（实际清理）
# ============================================================


def test_purge_removes_old_jsonl(ready_config: Path) -> None:
    """purge 应删除超过 retention_days 的 JSONL"""
    from datetime import date, timedelta

    records_dir = ready_config / "records"
    records_dir.mkdir(parents=True, exist_ok=True)

    old_date = (date.today() - timedelta(days=35)).isoformat()
    new_date = date.today().isoformat()
    old_file = records_dir / f"records-{old_date}.jsonl"
    new_file = records_dir / f"records-{new_date}.jsonl"
    old_file.write_text("old\n")
    new_file.write_text("new\n")

    result = runner.invoke(app, ["purge", "--retention-days", "30", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["data"]["dry_run"] is False
    assert old_file.name in data["data"]["removed_jsonl_files"]
    assert not old_file.exists()
    assert new_file.exists()


def test_purge_dry_run_preserves_files(ready_config: Path) -> None:
    """--dry-run 不实际删除"""
    from datetime import date, timedelta

    records_dir = ready_config / "records"
    records_dir.mkdir(parents=True, exist_ok=True)

    old_date = (date.today() - timedelta(days=35)).isoformat()
    old_file = records_dir / f"records-{old_date}.jsonl"
    old_file.write_text("old\n")

    result = runner.invoke(app, ["purge", "--retention-days", "30", "--dry-run", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["data"]["dry_run"] is True
    assert old_file.name in data["data"]["removed_jsonl_files"]
    assert old_file.exists()  # 文件未被删除


# ============================================================
# start/stop/restart（基础场景 mock subprocess）
# ============================================================


def test_start_already_running(ready_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """已有运行实例时 start 应拒绝"""
    from saitec.cli._common import write_pid, pid_file_path

    write_pid(ready_config / "config.json", os.getpid())  # 伪造当前进程为运行中

    result = runner.invoke(app, ["start", "--json"])
    assert result.exit_code == 2
    data = json.loads(result.stdout)
    assert data["ok"] is False
    assert "已在运行" in data["error"]["message"]


def test_start_config_not_found(isolated: Path) -> None:
    """config 不存在时 start 应报错"""
    result = runner.invoke(app, ["start", "--json"])
    assert result.exit_code == 2
    data = json.loads(result.stdout)
    assert "不存在" in data["error"]["message"]


def test_start_success_mock(ready_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """start 成功应返回 pid + 端口信息（mock subprocess）"""
    from unittest.mock import MagicMock

    mock_proc = MagicMock()
    mock_proc.pid = 12345

    def mock_popen(*args, **kwargs):
        return mock_proc

    monkeypatch.setattr("subprocess.Popen", mock_popen)

    result = runner.invoke(app, ["start", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["ok"] is True
    assert data["data"]["started"] is True
    assert data["data"]["pid"] == 12345
    assert len(data["data"]["services"]) == 1


def test_stop_not_running(ready_config: Path) -> None:
    """无 PID 文件时 stop 应报错"""
    result = runner.invoke(app, ["stop", "--json"])
    assert result.exit_code == 2
    data = json.loads(result.stdout)
    assert "未找到" in data["error"]["message"]


def test_stop_stale_pid(ready_config: Path) -> None:
    """PID 文件存在但进程已死应清理"""
    from saitec.cli._common import write_pid

    write_pid(ready_config / "config.json", 999999)  # 不存在的 PID

    result = runner.invoke(app, ["stop", "--json"])
    assert result.exit_code == 2
    data = json.loads(result.stdout)
    assert "已失效" in data["error"]["message"]


def test_restart_mock(ready_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """restart 应先 stop 再 start（mock subprocess）"""
    from unittest.mock import MagicMock

    mock_proc = MagicMock()
    mock_proc.pid = 54321

    def mock_popen(*args, **kwargs):
        return mock_proc

    monkeypatch.setattr("subprocess.Popen", mock_popen)

    result = runner.invoke(app, ["restart", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["ok"] is True
    assert data["data"]["restarted"] is True
    assert data["data"]["new_pid"] == 54321


# ============================================================
# logs/tail
# ============================================================


def test_logs_no_file(ready_config: Path) -> None:
    """logs 在日志不存在时应报错"""
    result = runner.invoke(app, ["logs", "--json"])
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert "不存在" in data["error"]["message"]


def test_logs_tail(ready_config: Path) -> None:
    """logs --tail 应返回最后 N 行"""
    log_dir = ready_config / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "safe-guard.log"
    log_file.write_text("\n".join([f"line {i}" for i in range(200)]))

    result = runner.invoke(app, ["logs", "--tail", "10", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["ok"] is True
    assert len(data["data"]) == 10
    assert "line 199" in data["data"][-1]


def test_logs_service_filter(ready_config: Path) -> None:
    """logs --service 应过滤"""
    log_dir = ready_config / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "safe-guard.log"
    log_file.write_text("svc-a: hello\nsvc-b: world\nsvc-a: bye\n")

    result = runner.invoke(app, ["logs", "--service", "svc-a", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert len(data["data"]) == 2
    assert "svc-a" in data["data"][0]


def test_tail_no_records(ready_config: Path) -> None:
    """tail 在 records 不存在时应报错"""
    result = runner.invoke(app, ["tail", "--json"])
    assert result.exit_code == 1
    data = json.loads(result.stdout)
    assert "不存在" in data["error"]["message"] or "无 records" in data["error"]["message"]
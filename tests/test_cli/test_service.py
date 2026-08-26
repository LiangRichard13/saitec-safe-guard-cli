"""service 子命令组 + init 新行为 + upstream 防呆测试"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ssgc.cli.main import app

runner = CliRunner()


@pytest.fixture
def ready_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """初始化一个单服务配置（upstream 指向本地 mock 上游）"""
    cfg_path = tmp_path / "config.json"
    monkeypatch.setenv("SSGC_CONFIG", str(cfg_path))
    monkeypatch.delenv("SSGC_API_KEY", raising=False)
    monkeypatch.delenv("SSGC_DETECTOR_URL", raising=False)
    r = runner.invoke(app, [
        "init", "--api-key", "sk-test-12345678",
        "--detector-url", "http://d:8080",
        "--upstream", "http://127.0.0.1:9101",
    ])
    assert r.exit_code == 0, r.stderr
    return tmp_path


def _services(cfg_dir: Path) -> list[dict]:
    return json.loads((cfg_dir / "config.json").read_text(encoding="utf-8"))["services"]


# ============================================================
# init 新行为
# ============================================================


def test_init_missing_upstream_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """非 TTY 且未给 --upstream → MISSING_UPSTREAM"""
    monkeypatch.setenv("SSGC_CONFIG", str(tmp_path / "config.json"))
    result = runner.invoke(
        app, ["init", "--api-key", "sk-test-12345678", "--detector-url", "http://d:8080"],
        input="\n",
    )
    assert result.exit_code == 1
    assert "缺少 upstream" in result.stderr
    assert not (tmp_path / "config.json").exists()  # 未写入


def test_init_single_service_with_upstream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """init --upstream 生成单服务配置"""
    monkeypatch.setenv("SSGC_CONFIG", str(tmp_path / "config.json"))
    result = runner.invoke(
        app, [
            "init", "--api-key", "sk-test-12345678",
            "--detector-url", "http://d:8080",
            "--upstream", "http://localhost:23333",
        ],
    )
    assert result.exit_code == 0
    cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert len(cfg["services"]) == 1
    svc = cfg["services"][0]
    assert svc["upstream"] == "http://localhost:23333"
    assert svc["name"] == "openai-chat-completions"  # 缺省 name = endpoint_type
    assert svc["port"] == 9001


def test_init_guesses_anthropic_type(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """upstream 含 anthropic → 启发式 anthropic-messages"""
    monkeypatch.setenv("SSGC_CONFIG", str(tmp_path / "config.json"))
    result = runner.invoke(
        app, [
            "init", "--api-key", "sk-test-12345678",
            "--detector-url", "http://d:8080",
            "--upstream", "https://api.deepseek.com/anthropic",
        ],
    )
    assert result.exit_code == 0
    cfg = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert cfg["services"][0]["endpoint_type"] == "anthropic-messages"
    # 人类可读输出里说明是猜的
    assert "猜测" in result.stdout


def test_init_rejects_invalid_endpoint_type(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSGC_CONFIG", str(tmp_path / "config.json"))
    result = runner.invoke(
        app, [
            "init", "--api-key", "sk-test-12345678",
            "--detector-url", "http://d:8080",
            "--upstream", "http://x",
            "--endpoint-type", "bogus",
        ],
    )
    assert result.exit_code == 1
    assert "endpoint_type" in result.stderr


# ============================================================
# service add
# ============================================================


def test_service_add_defaults(ready_config: Path) -> None:
    """add 默认端口 9002（9001 已被 init 占）、启发式 type"""
    result = runner.invoke(app, [
        "service", "add", "local-llm", "--upstream", "http://localhost:23333", "--json",
    ])
    assert result.exit_code == 0, result.stderr
    svcs = _services(ready_config)
    assert len(svcs) == 2
    added = svcs[1]
    assert added["name"] == "local-llm"
    assert added["port"] == 9002  # 9001 已占 → 递增
    assert added["endpoint_type"] == "openai-chat-completions"  # URL 无 anthropic → 默认
    data = json.loads(result.stdout)
    assert data["data"]["endpoint_type_guessed"] is True


def test_service_add_explicit_type_and_port(ready_config: Path) -> None:
    result = runner.invoke(app, [
        "service", "add", "deepseek",
        "--upstream", "https://api.deepseek.com/anthropic",
        "--endpoint-type", "anthropic-messages",
        "--port", "9050",
    ])
    assert result.exit_code == 0, result.stderr
    added = _services(ready_config)[1]
    assert added["name"] == "deepseek"
    assert added["port"] == 9050
    assert added["endpoint_type"] == "anthropic-messages"


def test_service_add_duplicate_name(ready_config: Path) -> None:
    result = runner.invoke(app, [
        "service", "add", "openai-chat-completions", "--upstream", "http://x",
    ])
    assert result.exit_code == 1
    assert "已存在" in result.stderr
    assert len(_services(ready_config)) == 1  # 未新增


def test_service_add_invalid_upstream(ready_config: Path) -> None:
    """非法 scheme → 校验失败不写入"""
    result = runner.invoke(app, [
        "service", "add", "bad", "--upstream", "ftp://x",
    ])
    assert result.exit_code == 1
    assert len(_services(ready_config)) == 1


def test_service_add_duplicate_port(ready_config: Path) -> None:
    """端口与已有服务冲突 → 校验失败"""
    result = runner.invoke(app, [
        "service", "add", "dup-port", "--upstream", "http://x", "--port", "9001",
    ])
    assert result.exit_code == 1
    assert len(_services(ready_config)) == 1


# ============================================================
# service remove / set
# ============================================================


def test_service_remove(ready_config: Path) -> None:
    runner.invoke(app, ["service", "add", "extra", "--upstream", "http://x:1"])
    result = runner.invoke(app, ["service", "remove", "extra"])
    assert result.exit_code == 0
    names = [s["name"] for s in _services(ready_config)]
    assert "extra" not in names


def test_service_remove_not_found(ready_config: Path) -> None:
    result = runner.invoke(app, ["service", "remove", "nope"])
    assert result.exit_code == 1
    assert "不存在" in result.stderr


def test_service_set_multiple_fields(ready_config: Path) -> None:
    result = runner.invoke(app, [
        "service", "set", "openai-chat-completions",
        "--upstream", "https://api.deepseek.com",
        "--port", "9200",
    ])
    assert result.exit_code == 0, result.stderr
    svc = _services(ready_config)[0]
    assert svc["upstream"] == "https://api.deepseek.com"
    assert svc["port"] == 9200


def test_service_set_no_changes(ready_config: Path) -> None:
    result = runner.invoke(app, ["service", "set", "openai-chat-completions"])
    assert result.exit_code == 1
    assert "未指定任何修改" in result.stderr


def test_service_set_not_found(ready_config: Path) -> None:
    result = runner.invoke(app, ["service", "set", "nope", "--port", "9300"])
    assert result.exit_code == 1
    assert "不存在" in result.stderr


# ============================================================
# service list
# ============================================================


def test_service_list_human(ready_config: Path) -> None:
    result = runner.invoke(app, ["service", "list"])
    assert result.exit_code == 0
    assert "服务映射" in result.stdout
    assert "http://127.0.0.1:9101" in result.stdout  # 显示 upstream
    assert "OPENAI_BASE_URL=http://127.0.0.1:9001/v1" in result.stdout  # 客户端提示


def test_service_list_json(ready_config: Path) -> None:
    result = runner.invoke(app, ["service", "list", "--json"])
    data = json.loads(result.stdout)
    assert data["ok"] is True
    assert data["data"]["count"] == 1
    assert data["data"]["services"][0]["upstream"] == "http://127.0.0.1:9101"


# ============================================================
# upstream 防呆警告
# ============================================================


def test_service_add_warns_on_endpoint_suffix(ready_config: Path) -> None:
    """upstream 配成完整端点 URL → 警告但写入成功"""
    result = runner.invoke(app, [
        "service", "add", "bad-upstream",
        "--upstream", "http://localhost:23333/v1/chat/completions",
        "--json",
    ])
    assert result.exit_code == 0  # 不阻断
    data = json.loads(result.stdout)
    assert data["ok"] is True
    assert any("路径重复" in w for w in data["data"].get("warnings", []))


def test_config_set_upstream_warns(ready_config: Path) -> None:
    """config set services.*.upstream 也有防呆警告"""
    result = runner.invoke(app, [
        "config", "set", "services.openai-chat-completions.upstream",
        "http://localhost:23333/v1/chat/completions", "--json",
    ])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert any("路径重复" in w for w in data["data"].get("warnings", []))


def test_no_warning_on_clean_upstream(ready_config: Path) -> None:
    """干净的 base URL 前缀（可含路径）不告警"""
    result = runner.invoke(app, [
        "service", "add", "zen",
        "--upstream", "https://opencode.ai/zen/go/v1",
        "--json",
    ])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert not data["data"].get("warnings")


# ============================================================
# format_services_block 单元
# ============================================================


def test_format_services_block_variants() -> None:
    from ssgc.cli._common import format_services_block

    block = format_services_block([
        {"name": "a", "port": 9001, "upstream": "http://u1",
         "endpoint_type": "openai-chat-completions"},
        {"name": "b", "port": 9003, "upstream": "http://u2/anthropic",
         "endpoint_type": "anthropic-messages"},
    ])
    assert "OPENAI_BASE_URL=http://127.0.0.1:9001/v1" in block
    assert "ANTHROPIC_BASE_URL=http://127.0.0.1:9003" in block
    assert "http://u2/anthropic" in block

    empty = format_services_block([])
    assert "无监控服务" in empty

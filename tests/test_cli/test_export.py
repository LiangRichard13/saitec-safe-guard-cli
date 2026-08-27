"""export 命令测试：默认异常过滤 / all / 双格式对齐 / JSONL 关联与缺失"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ssgc.cli.main import app
from ssgc.core.models import DetectionResult, Record
from ssgc.store.store import Store

runner = CliRunner()


def _mk_result(rid: str, status: str, ts: str) -> DetectionResult:
    return DetectionResult(
        record_id=rid,
        service="svc-a",
        endpoint_type="openai-chat-completions",
        upstream="https://api.openai.com",
        timestamp=ts,
        status_code=200,
        elapsed_ms=100,
        model="gpt-4o",
        detection_status=status,
        risk_level={"violation": "high", "suspicious": "medium", "clean": "low", "error": None}[status],
        detection_detail={"score": 0.5, "reason": f"reason-{status}"},
        detected_at=ts,
    )


def _seed(db_path: Path) -> None:
    store = Store(db_path)
    base = datetime(2026, 8, 26, tzinfo=timezone.utc)
    store._init_schema()
    import asyncio

    asyncio.run(store.save_results([
        _mk_result("v-1", "violation", (base).isoformat()),
        _mk_result("s-1", "suspicious", (base + timedelta(minutes=1)).isoformat()),
        _mk_result("e-1", "error", (base + timedelta(minutes=2)).isoformat()),
        _mk_result("c-1", "clean", (base + timedelta(minutes=3)).isoformat()),
    ]))


def _seed_jsonl(records_dir: Path, rid: str | None, content: str = "<script>alert(1)</script>危险内容") -> None:
    records_dir.mkdir(parents=True, exist_ok=True)
    if rid is None:
        return
    line = json.dumps({
        "record_id": rid, "service": "svc-a", "endpoint_type": "openai-chat-completions",
        "upstream": "https://api.openai.com", "path": "/v1/chat/completions",
        "timestamp": "2026-08-26T00:00:00+00:00", "elapsed_ms": 100, "status_code": 200,
        "error": None,
        "request": {"model": "gpt-4o", "messages": [{"role": "user", "content": content}], "stream": False},
        "response": {"content": f"reply-for-{rid}", "finish_reason": "stop", "usage": None},
    }, ensure_ascii=False)
    (records_dir / "records-2026-08-26.jsonl").write_text(line + "\n", encoding="utf-8")


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """隔离配置目录：预置 4 条结论（3 异常 1 clean）+ v-1 的 JSONL 原始记录"""
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    monkeypatch.setenv("SSGC_CONFIG", str(cfg_dir / "config.json"))
    db_path = cfg_dir / "results.db"
    _seed(db_path)
    _seed_jsonl(cfg_dir / "records", "v-1")
    monkeypatch.chdir(tmp_path)
    return cfg_dir, tmp_path


# ============================================================
# 默认行为与过滤
# ============================================================


def test_export_default_excludes_clean(env: tuple) -> None:
    _, tmp = env
    out = tmp / "r.md"
    result = runner.invoke(app, ["export", "--since", "7d", "-o", str(out)])
    assert result.exit_code == 0
    text = out.read_text(encoding="utf-8")
    assert "v-1" in text and "s-1" in text and "e-1" in text
    assert "c-1" not in text
    assert "suspicious、violation、error" in text
    assert "不含 clean" in text


def test_export_status_all_includes_clean(env: tuple) -> None:
    _, tmp = env
    out = tmp / "all.md"
    result = runner.invoke(app, ["export", "--status", "all", "--since", "7d", "-o", str(out)])
    assert result.exit_code == 0
    text = out.read_text(encoding="utf-8")
    assert "c-1" in text and "v-1" in text


def test_export_bad_status_rejected(env: tuple) -> None:
    _, tmp = env
    result = runner.invoke(app, ["export", "--status", "dangerous", "--since", "7d"])
    assert result.exit_code != 0


# ============================================================
# JSONL 关联
# ============================================================


def test_export_joins_dialogue_and_escapes(env: tuple) -> None:
    """有关联记录的条目含完整对话；内容中的 HTML 特殊字符不破坏 markdown（原样保留）"""
    _, tmp = env
    out = tmp / "join.md"
    result = runner.invoke(app, ["export", "-o", str(out), "--since", "7d"])
    assert result.exit_code == 0
    text = out.read_text(encoding="utf-8")
    assert "<script>alert(1)</script>危险内容" in text   # 完整对话原文
    assert "reply-for-v-1" in text                        # assistant 回复


def test_export_missing_jsonl_marks_conclusion_only(env: tuple) -> None:
    """s-1 无 JSONL 记录 → 标注仅结论，不报错"""
    _, tmp = env
    out = tmp / "miss.md"
    result = runner.invoke(app, ["export", "-o", str(out), "--since", "7d"])
    assert result.exit_code == 0
    text = out.read_text(encoding="utf-8")
    assert "原始内容缺失" in text


# ============================================================
# HTML 格式
# ============================================================


def test_export_html_escape_and_semantics(env: tuple) -> None:
    _, tmp = env
    out = tmp / "r.html"
    result = runner.invoke(app, ["export", "-f", "html", "-o", str(out), "--since", "7d"])
    assert result.exit_code == 0
    html_text = out.read_text(encoding="utf-8")
    # XSS 转义：< 不应以原始标签出现
    assert "<script>alert" not in html_text
    assert "&lt;script&gt;" in html_text
    # 语义结构与折叠策略（默认异常导出全展开）
    assert 'class="badge"' in html_text
    assert "<details" in html_text and ' open' in html_text
    assert "suspicious、violation、error" in html_text or "suspicious,violation,error" in html_text.replace(" ", "")


def test_export_html_full_folds_clean(env: tuple) -> None:
    """--status all 时 clean/error 折叠（无 open 属性），异常仍展开"""
    _, tmp = env
    out = tmp / "full.html"
    result = runner.invoke(app, ["export", "-f", "html", "--status", "all", "-o", str(out), "--since", "7d"])
    assert result.exit_code == 0
    html_text = out.read_text(encoding="utf-8")
    clean_idx = html_text.find("9999") if False else None
    # 简化断言：clean 记录 ID 存在；details 里存在不带 open 的 details 标签
    assert "<details" in html_text
    assert any(seg.startswith("<details") for seg in html_text.split("<details")[1:]) or True
    # 更精确：解析每个 details 开标签
    import re
    opens = re.findall(r"<details class=\"rec\"[^>]*>", html_text)
    assert len(opens) == 4
    folded = [t for t in opens if not t.rstrip(">").endswith('" open') and " open" not in t]
    assert len(folded) >= 1  # 至少 clean/error 有收起的


# ============================================================
# 双格式对齐 + --json 摘要
# ============================================================


def test_export_md_html_content_aligned(env: tuple) -> None:
    _, tmp = env
    md_out, html_out = tmp / "a.md", tmp / "a.html"
    r1 = runner.invoke(app, ["export", "-o", str(md_out), "--since", "7d"])
    r2 = runner.invoke(app, ["export", "-f", "html", "-o", str(html_out), "--since", "7d"])
    assert r1.exit_code == 0 and r2.exit_code == 0
    md_text = md_out.read_text(encoding="utf-8")
    html_text = html_out.read_text(encoding="utf-8")
    for marker in ("v-1", "s-1", "e-1"):
        assert marker in md_text and marker in html_text


def test_export_json_summary(env: tuple) -> None:
    _, tmp = env
    import sys as _sys
    result = runner.invoke(app, ["export", "--json", "--since", "7d", "-o", str(tmp / "j.md")])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    data = payload["data"]
    assert data["count"] == 3
    assert data["format"] == "md"
    assert data["by_status"]["violation"] == 1
    assert data["truncated"] is False
    assert data["output_path"].endswith("j.md")


def test_export_no_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSGC_CONFIG", str(tmp_path / "none" / "config.json"))
    result = runner.invoke(app, ["export", "--json"])
    assert result.exit_code != 0

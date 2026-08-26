"""mock 检测服务器（tests/mock_detector/server.py）自身接口测试

用 fastapi 的 TestClient 直接调 app，验证 `/health` `/detect` `/records` 契约。
依赖 fastapi（可选依赖 `[mock]`），未安装时整个模块被 `pytest.importorskip` 跳过。
"""
from __future__ import annotations

import pytest

# fastapi 是可选依赖 `[mock]`。核心测试套件只装 `[dev]` 时不装 fastapi，
# 这里 importorskip 让本模块在缺依赖时整体跳过，不影响核心套件收集。
pytest.importorskip("fastapi")
pytest.importorskip("starlette.testclient")

import os
import sys
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

# 让 mock_detector 可作为包导入（tests/mock_detector 是相对 tests/ 的兄弟目录，
# 但 pytest 以 rootdir 为基准收集，因此用 sys.path 显式加入）
_MOCK_DIR = Path(__file__).resolve().parent.parent / "mock_detector"
if str(_MOCK_DIR) not in sys.path:
    sys.path.insert(0, str(_MOCK_DIR))

import server as mock_server  # noqa: E402


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """重置内存存储与检测模式，返回 TestClient

    强制 DETECTION_MODE=random：tests/mock_detector/.env 可能配置了 llm 模式
    （server.py import 时读取），测试需要确定性的 random 行为，不受用户本地 .env 影响。
    """
    monkeypatch.setattr(mock_server, "DETECTION_MODE", "random")
    mock_server._records = []  # noqa: SLF001  （重启清空，等价于新起实例）
    with TestClient(mock_server.app) as c:
        yield c


# ============================================================
# /health
# ============================================================


def test_health_ok(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["violation_rate"] == pytest.approx(0.05)
    assert body["stored_records"] == 0


# ============================================================
# /detect — 鉴权
# ============================================================


def _batch_payload(n: int = 3) -> dict:
    return {
        "batch": [
            {
                "record_id": f"r{i}",
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
            for i in range(n)
        ]
    }


def test_detect_missing_api_key_401(client: TestClient) -> None:
    resp = client.post("/detect", json=_batch_payload())
    assert resp.status_code == 401


def test_detect_wrong_api_key_401(client: TestClient) -> None:
    resp = client.post(
        "/detect", json=_batch_payload(), headers={"X-API-Key": "wrong"}
    )
    assert resp.status_code == 401


def test_detect_ok_returns_matching_results(client: TestClient) -> None:
    resp = client.post(
        "/detect", json=_batch_payload(3), headers={"X-API-Key": "mock-test-key"}
    )
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert len(results) == 3
    # 每个 result 的 record_id 与入参对应，且带检测字段
    assert {r["record_id"] for r in results} == {"r0", "r1", "r2"}
    for r in results:
        assert r["detection_status"] in ("clean", "violation")
        assert "detected_at" in r


def test_detect_empty_batch(client: TestClient) -> None:
    resp = client.post(
        "/detect", json={"batch": []}, headers={"X-API-Key": "mock-test-key"}
    )
    assert resp.status_code == 200
    assert resp.json()["results"] == []


# ============================================================
# 5% 危险标记逻辑（用 VIOLATION_RATE 边界确定性验证）
# ============================================================


def test_violation_rate_edge_0_never_violates(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """VIOLATION_RATE=0 → 永不 violation"""
    monkeypatch.setattr(mock_server, "VIOLATION_RATE", 0.0)
    resp = client.post(
        "/detect", json=_batch_payload(10), headers={"X-API-Key": "mock-test-key"}
    )
    results = resp.json()["results"]
    assert all(r["detection_status"] == "clean" for r in results)
    assert all(r["risk_level"] == "low" for r in results)


def test_violation_rate_edge_1_always_violates(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """VIOLATION_RATE=1 → 全部 violation，risk_level ∈ {high, critical}"""
    monkeypatch.setattr(mock_server, "VIOLATION_RATE", 1.0)
    resp = client.post(
        "/detect", json=_batch_payload(10), headers={"X-API-Key": "mock-test-key"}
    )
    results = resp.json()["results"]
    assert all(r["detection_status"] == "violation" for r in results)
    assert all(r["risk_level"] in ("high", "critical") for r in results)
    assert all(r["detection_detail"]["score"] >= 0.7 for r in results)


def test_violation_statistics_about_five_percent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """大样本下 violation 比例接近 5%（用固定随机串来拿确定性结果）

    用 monkeypatch 让 random.random 落在 [0,0.1) 区间内均匀取值，
    验证只有 <0.05 的样本被标 violation。为免 mock 服务器内部 random 多调用
    干扰，这里不追求精确，仅验证"部分 hit、部分 clean"且 detail.score 分档。
    """
    # 用固定的小 random 值(0.01) -> 必 violation；(0.09) -> 必 clean
    monkeypatch.setattr(mock_server.random, "random", lambda: 0.01)
    resp_hit = client.post(
        "/detect", json=_batch_payload(1), headers={"X-API-Key": "mock-test-key"}
    )
    assert resp_hit.json()["results"][0]["detection_status"] == "violation"

    mock_server._records = []  # noqa: SLF001
    monkeypatch.setattr(mock_server.random, "random", lambda: 0.09)
    resp_clean = client.post(
        "/detect", json=_batch_payload(1), headers={"X-API-Key": "mock-test-key"}
    )
    assert resp_clean.json()["results"][0]["detection_status"] == "clean"


# ============================================================
# /records
# ============================================================


@pytest.fixture
def client_with_records(client: TestClient) -> TestClient:
    """用 violation_rate=1 灌 5 条 violation（svc-a），再用 rate=0 灌 5 条 clean（svc-b）"""
    def _single(rid: str, service: str) -> dict:
        return {"batch": [{
            "record_id": rid, "service": service,
            "endpoint_type": "t", "upstream": "u", "path": "/p",
            "timestamp": "2026-08-20T10:00:00Z", "elapsed_ms": 1,
            "status_code": 200, "request": {}, "response": {},
        }]}

    with mock.patch.object(mock_server, "VIOLATION_RATE", 1.0):
        for i in range(5):
            client.post(
                "/detect", json=_single(f"v{i}", "svc-a"),
                headers={"X-API-Key": "mock-test-key"},
            )
    with mock.patch.object(mock_server, "VIOLATION_RATE", 0.0):
        for i in range(5):
            client.post(
                "/detect", json=_single(f"c{i}", "svc-b"),
                headers={"X-API-Key": "mock-test-key"},
            )
    return client


def test_records_count_and_violations(client_with_records: TestClient) -> None:
    resp = client_with_records.get("/records", params={"limit": 1000})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 10
    assert body["violations"] == 5


def test_records_filter_by_service(client_with_records: TestClient) -> None:
    resp = client_with_records.get("/records", params={"service": "svc-a", "limit": 1000})
    body = resp.json()
    assert body["count"] == 5
    assert all(r["service"] == "svc-a" for r in body["records"])


def test_records_filter_by_risk(client_with_records: TestClient) -> None:
    """svc-a 全 high/critical（violation），按 risk=high 应只命中子集"""
    resp = client_with_records.get("/records", params={"risk": "high", "limit": 1000})
    body = resp.json()
    assert body["count"] >= 1
    assert all(r["risk_level"] == "high" for r in body["records"])


def test_records_limit(client_with_records: TestClient) -> None:
    resp = client_with_records.get("/records", params={"limit": 3})
    body = resp.json()
    assert body["count"] == 3
    assert len(body["records"]) == 3


# ============================================================
# llm 检测模式
# ============================================================


def test_llm_mode_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """MOCK_DETECTION_MODE=llm 且无 MOCK_LLM_API_KEY → 启动（import）即退出"""
    import importlib

    # 用户本地 .env 可能已配 llm + key（import 时读取），此时该测试无意义
    if (_MOCK_DIR / ".env").exists():
        pytest.skip("tests/mock_detector/.env 存在（含真实配置），跳过 import 校验测试")

    monkeypatch.setenv("MOCK_DETECTION_MODE", "llm")
    monkeypatch.delenv("MOCK_LLM_API_KEY", raising=False)
    try:
        with pytest.raises(SystemExit, match="MOCK_LLM_API_KEY"):
            importlib.reload(mock_server)  # 模块级常量在 import 时读 env 并校验
    finally:
        monkeypatch.undo()          # 先还原 env
        importlib.reload(mock_server)  # 再 reload 恢复默认 random 模块状态


def test_detect_llm_mode_dispatch(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """llm 模式下 detect 走 _evaluate_llm（mock 为固定结论）"""

    async def fake_evaluate(record: dict) -> dict:
        return {
            "record_id": record.get("record_id"),
            "detection_status": "violation",
            "risk_level": "high",
            "detection_detail": {"detector": "llm", "reason": "prompt injection"},
            "detected_at": "2026-08-22T00:00:00Z",
        }

    monkeypatch.setattr(mock_server, "DETECTION_MODE", "llm")
    monkeypatch.setattr(mock_server, "_evaluate_llm", fake_evaluate)

    resp = client.post(
        "/detect", json=_batch_payload(2), headers={"X-API-Key": "mock-test-key"}
    )
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert len(results) == 2
    assert all(r["detection_status"] == "violation" for r in results)
    assert all(r["detection_detail"]["detector"] == "llm" for r in results)


def test_evaluate_llm_degrades_on_network_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLM 调用失败 → error 结论（不抛异常、不阻断上报）"""

    class _Boom:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            raise ConnectionError("network down")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(mock_server.httpx, "AsyncClient", _Boom)

    import asyncio
    result = asyncio.run(mock_server._evaluate_llm({"record_id": "r1"}))  # noqa: SLF001
    assert result["detection_status"] == "error"
    assert "llm_check_failed" in result["detection_detail"]["reason"]


def test_evaluate_llm_parses_markdown_wrapped_json(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLM 返回 markdown 代码块包裹的 JSON 也能解析"""

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content":
                '```json\n{"detection_status": "violation", "risk_level": "high", "reason": "PII"}\n```'
            }}]}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _FakeResp()

    monkeypatch.setattr(mock_server.httpx, "AsyncClient", _FakeClient)

    import asyncio
    result = asyncio.run(mock_server._evaluate_llm({"record_id": "r2"}))  # noqa: SLF001
    assert result["detection_status"] == "violation"
    assert result["risk_level"] == "high"
    assert result["detection_detail"]["reason"] == "PII"


# ============================================================
# llm 模式：前缀 hash 缓存去重（同会话 O(N²) → O(N)）
# ============================================================


class _RecordingLLM:
    """假 LLM：记录每次收到的 user 内容，返回固定 clean 结论"""

    calls: list[str] = []

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content":
                '{"detection_status": "clean", "risk_level": "low", "reason": "pass"}'}}]}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, **k):
        _RecordingLLM.calls.append(json["messages"][1]["content"])
        return self._Resp()


@pytest.fixture
def llm_dedup_env(monkeypatch: pytest.MonkeyPatch):
    """清空缓存与统计 + 挂假 LLM"""
    mock_server._prefix_cache.clear()  # noqa: SLF001
    for k in mock_server._cache_stats:  # noqa: SLF001
        mock_server._cache_stats[k] = 0  # noqa: SLF001
    _RecordingLLM.calls.clear()
    monkeypatch.setattr(mock_server.httpx, "AsyncClient", _RecordingLLM)
    monkeypatch.setattr(mock_server, "DETECTION_MODE", "llm")


def _session_record(rid: str, turns: list[dict]) -> dict:
    """构造同会话递增 Record：第 N 条含前 N 轮全部 messages"""
    return {
        "record_id": rid,
        "request": {"messages": turns},
        "response": {"content": f"reply-{rid}"},
    }


def test_llm_prefix_cache_dedup(llm_dedup_env) -> None:
    """同会话递增上报：LLM 只收新增轮次，轮次审读 O(N) 而非 O(N²)"""
    import asyncio

    t1 = {"role": "user", "content": "第一轮问题"}
    t2 = {"role": "assistant", "content": "第一轮回答"}
    t3 = {"role": "user", "content": "第二轮问题"}

    # Record1: 1 轮（miss，全量审）
    r1 = asyncio.run(mock_server._evaluate_llm(_session_record("r1", [t1])))  # noqa: SLF001
    assert r1["detection_status"] == "clean"
    assert mock_server._cache_stats["misses"] == 1  # noqa: SLF001
    assert len(_RecordingLLM.calls) == 1

    # Record2: 前缀包含 Record1 + 新增 2 轮（partial，只审新增）
    r2 = asyncio.run(mock_server._evaluate_llm(_session_record("r2", [t1, t2, t3])))  # noqa: SLF001
    assert mock_server._cache_stats["partial_hits"] == 1  # noqa: SLF001
    assert len(_RecordingLLM.calls) == 2
    # LLM 收到的内容：前情提示 + 新增两轮，且不含第一轮旧内容（截断内容里的 key 片段验证）
    sent = _RecordingLLM.calls[1]
    assert "前情提示" in sent and "clean" in sent
    assert "第一轮问题" not in sent          # 旧轮不重发
    assert "第二轮问题" in sent               # 新轮送审
    assert r2["detection_detail"]["new_turns"] == 2

    # Record3: 完全重放 Record2 的会话（full hit，不调 LLM）
    r3 = asyncio.run(mock_server._evaluate_llm(_session_record("r3", [t1, t2, t3])))  # noqa: SLF001
    assert mock_server._cache_stats["full_hits"] == 1  # noqa: SLF001
    assert len(_RecordingLLM.calls) == 2                # 没有新调用
    assert r3["detection_status"] == "clean"
    assert r3["detection_detail"]["cache"] == "full"

    # 统计：3 条 Record 共 1+3+3=7 轮到达，实送 1+2+0=3 轮
    s = mock_server._cache_stats  # noqa: SLF001
    assert s["turns_total"] == 7
    assert s["turns_sent"] == 3
    assert s["llm_calls"] == 2


def test_llm_prefix_cache_independent_sessions(llm_dedup_env) -> None:
    """不同会话（前缀不同）互不命中"""
    import asyncio

    a = asyncio.run(mock_server._evaluate_llm(_session_record(  # noqa: SLF001
        "a1", [{"role": "user", "content": "会话A第一轮"}])))
    b = asyncio.run(mock_server._evaluate_llm(_session_record(  # noqa: SLF001
        "b1", [{"role": "user", "content": "会话B第一轮"}])))
    assert mock_server._cache_stats["misses"] == 2  # noqa: SLF001
    assert len(_RecordingLLM.calls) == 2
    assert a["detection_status"] == "clean" and b["detection_status"] == "clean"

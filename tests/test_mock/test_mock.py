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
def client() -> TestClient:
    """重置内存存储，返回 TestClient"""
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

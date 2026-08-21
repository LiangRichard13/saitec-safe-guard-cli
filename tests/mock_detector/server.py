"""Mock 检测服务器（FastAPI）

模拟单位内部安全检测服务的接口：接收 /detect 的批量记录，
按 `MOCK_DETECTION_VIOLATION_RATE`（默认 5%）概率把每条记录标为危险，
其余标为 clean。

提供：
- POST /detect  接收上报
- GET  /records 查询已处理记录（内存 list）
- GET  /health  健康检查

环境变量：
- MOCK_DETECTION_VIOLATION_RATE  危险概率，默认 0.05
- MOCK_DETECTION_LATENCY_MS       模拟处理延迟，默认 50
- MOCK_DETECTION_API_KEY           X-API-Key 期望值，默认 "mock-test-key"

⚠️ 仅用于本地开发 / 联调测试，**不要**用于生产。
"""
from __future__ import annotations

import asyncio
import os
import random
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request

VIOLATION_RATE = float(os.environ.get("MOCK_DETECTION_VIOLATION_RATE", "0.05"))
LATENCY_MS = int(os.environ.get("MOCK_DETECTION_LATENCY_MS", "50"))
API_KEY = os.environ.get("MOCK_DETECTION_API_KEY", "mock-test-key")

_VIOLATION_REASONS = ["prompt_injection", "pii_leakage", "policy_violation", "unsafe_completion"]

app = FastAPI(title="Saitec Mock Detector", version="0.1.0")

# 内存存储（重启清空）
_records: list[dict[str, Any]] = []


def _evaluate(record: dict[str, Any]) -> dict[str, Any]:
    """按概率给单条记录打分（5% 标 violation，其余 clean）"""
    is_violation = random.random() < VIOLATION_RATE

    if is_violation:
        detection_status = "violation"
        risk_level = random.choice(["high", "critical"])
        detection_detail: dict[str, Any] = {
            "score": round(0.7 + random.random() * 0.3, 3),
            "reason": random.choice(_VIOLATION_REASONS),
            "matched_rules": [
                f"rule-{uuid.uuid4().hex[:8]}" for _ in range(random.randint(1, 3))
            ],
            "trace_id": uuid.uuid4().hex,
        }
    else:
        detection_status = "clean"
        risk_level = "low"
        detection_detail = {
            "score": round(random.random() * 0.3, 3),
            "reason": "pass",
        }

    return {
        "record_id": record.get("record_id"),
        "detection_status": detection_status,
        "risk_level": risk_level,
        "detection_detail": detection_detail,
        "detected_at": datetime.now(timezone.utc).isoformat(),
    }


def _check_api_key(request: Request) -> None:
    if request.headers.get("X-API-Key") != API_KEY:
        raise HTTPException(status_code=401, detail="unauthorized")


@app.get("/health")
def health() -> dict[str, Any]:
    """健康检查（不校验 api_key）"""
    return {
        "ok": True,
        "violation_rate": VIOLATION_RATE,
        "latency_ms": LATENCY_MS,
        "stored_records": len(_records),
    }


@app.post("/detect")
async def detect(request: Request) -> dict[str, Any]:
    """接收批量记录，按概率标记危险"""
    _check_api_key(request)
    body = await request.json()
    batch = body.get("batch", [])

    if LATENCY_MS > 0:
        await asyncio.sleep(LATENCY_MS / 1000.0)

    results = [_evaluate(r) for r in batch]
    received_at = datetime.now(timezone.utc).isoformat()
    for r, result in zip(batch, results):
        _records.append(
            {
                "received_at": received_at,
                "record_id": r.get("record_id"),
                "service": r.get("service"),
                "endpoint_type": r.get("endpoint_type"),
                "upstream": r.get("upstream"),
                "path": r.get("path"),
                "status_code": r.get("status_code"),
                "elapsed_ms": r.get("elapsed_ms"),
                "timestamp": r.get("timestamp"),
                "detection_status": result["detection_status"],
                "risk_level": result["risk_level"],
                "detection_detail": result["detection_detail"],
            }
        )
    return {"results": results}


@app.get("/records")
def records(
    service: str | None = Query(None, description="按 service 过滤"),
    risk: str | None = Query(None, description="按 risk_level 过滤（low/medium/high/critical）"),
    limit: int = Query(100, ge=1, le=1000),
) -> dict[str, Any]:
    """查询已处理记录（内存 list，重启清空）"""
    filtered = _records
    if service is not None:
        filtered = [r for r in filtered if r.get("service") == service]
    if risk is not None:
        filtered = [r for r in filtered if r.get("risk_level") == risk]
    filtered = filtered[-limit:]  # 最新 limit 条
    violations = sum(1 for r in filtered if r.get("detection_status") == "violation")
    return {
        "count": len(filtered),
        "violations": violations,
        "records": filtered,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server:app",
        host=os.environ.get("MOCK_DETECTION_HOST", "127.0.0.1"),
        port=int(os.environ.get("MOCK_DETECTION_PORT", "8000")),
        reload=False,
    )

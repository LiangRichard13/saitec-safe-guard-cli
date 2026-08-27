"""手动联调脚本：经 ssgc 代理验证 openai-responses 协议全链路

【待补测】当前没有支持 Responses API 的真实上游——脚本已写好，等拿到
可用 key（OpenAI 官方 / 支持 /v1/responses 的网关）填入 .env 后运行。

不是 pytest 测试。用官方 openai SDK 的 responses 接口走真实客户端行为
（Authorization 头 + SSE 流式）。

运行：

    cd saitec-safe-guard-cli
    python tests/test_chat/probe_openai_responses.py [N]

配置（优先级：环境变量 > test_chat/.env > 默认值，三协议探针共享同一组变量）：

    TEST_BASE_URL      本地代理地址。要求 base_url 以 /v1 结尾
                       （openai SDK 在其后拼 /responses），如 http://127.0.0.1:9003/v1
    TEST_APIKEY        模型厂商的真实 API key（必填）
    TEST_MODEL_NAME    模型名（必填，须是该上游 Responses API 支持的模型）

准备对应 ssgc 服务（upstream 指向真实支持 Responses API 的端点）：

    ssgc service add openai-responses --endpoint-type openai-responses \
        --upstream <上游> --port 9003 --json
    ssgc restart --json
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from openai import OpenAI

_ENV_FILE = Path(__file__).resolve().parent / ".env"
_QUESTIONS_FILE = Path(__file__).resolve().parent / "test_questions.json"


def _load_dotenv(path: Path) -> None:
    """极简 .env 解析：KEY=VALUE 逐行，已存在的环境变量不覆盖"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def _load_prompts() -> list[str]:
    items = json.loads(_QUESTIONS_FILE.read_text(encoding="utf-8"))
    return [it["text"] for it in items]


def main() -> int:
    _load_dotenv(_ENV_FILE)

    base_url = os.environ.get("TEST_BASE_URL", "http://127.0.0.1:9003/v1")
    api_key = os.environ.get("TEST_APIKEY", "")
    model = os.environ.get("TEST_MODEL_NAME", "")

    if not api_key or not model:
        print("缺少配置：TEST_APIKEY / TEST_MODEL_NAME")
        print(f"请在 {_ENV_FILE} 填入后运行；上游需支持 OpenAI Responses API (/v1/responses)")
        return 1

    prompts_all = _load_prompts()
    n = int(sys.argv[1]) if len(sys.argv) > 1 else len(prompts_all)
    prompts = (prompts_all * 10)[:n]

    client = OpenAI(base_url=base_url, api_key=api_key)
    print(f"目标: {base_url}  模型: {model}  key: {api_key[:8]}***")
    print("=" * 70)

    ok = err = 0
    for i, p in enumerate(prompts, 1):
        t0 = time.time()
        try:
            stream = i > n - 2  # 最后 2 条流式（覆盖 SSE 路径），其余非流式
            if stream:
                chunks: list[str] = []
                with client.responses.create(
                    model=model,
                    input=p,
                    stream=True,
                ) as events:
                    for ev in events:
                        # response.output_text.done / output_text.delta 均可取；
                        # 统一从 delta 事件累积，验证完整 SSE 链路
                        if getattr(ev, "type", "") == "response.output_text.delta":
                            chunks.append(ev.delta or "")
                reply = "".join(chunks)
                mode = "流式"
            else:
                resp = client.responses.create(model=model, input=p)
                reply = resp.output_text or ""
                mode = "非流式"

            ok += 1
            print(f"[{i:2d}/{n}] {mode} ✓ {time.time()-t0:5.1f}s | {p[:18]}... → {reply[:40]!r}")
        except Exception as e:
            err += 1
            print(f"[{i:2d}/{n}] ✗ {time.time()-t0:5.1f}s | {p[:18]}... → 错误: {str(e)[:90]}")

    print("=" * 70)
    print(f"完成: 成功 {ok} / 失败 {err}")
    print("排查: 客户端解码类报错先看 JSONL 该条 response.content 是否完整（代理侧证据）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

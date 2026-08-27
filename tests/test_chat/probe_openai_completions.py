"""手动联调脚本：通过 ssgc 代理发消息，验证监控与上报链路

不是 pytest 测试（命名避开 test_ 前缀，pytest 不会收集），手动运行：

    cd saitec-safe-guard-cli
    python tests/test_chat/chat_probe.py       # 发 12 条（题库见 test_questions.json）
    python tests/test_chat/chat_probe.py 30    # 发 30 条（循环填充）

配置来源（优先级：环境变量 > test_chat/.env > 默认值）——三协议探针共享同一组变量：

    TEST_BASE_URL      本地代理地址。本脚本要求 base_url 以 /v1 结尾
                       （openai SDK 在其后拼 /chat/completions）
    TEST_APIKEY        模型厂商的真实 API key（必填）
    TEST_MODEL_NAME    模型名，如 deepseek-chat（必填）

在 test_chat/.env 填入后直接运行（.env 已被 .gitignore 忽略，不会提交）：

    TEST_APIKEY=sk-xxxxxxxx
    TEST_MODEL_NAME=deepseek-chat
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


def _load_prompts() -> list[str]:
    """题库统一放 test_questions.json（与另两个协议探针共享）"""
    items = json.loads(_QUESTIONS_FILE.read_text(encoding="utf-8"))
    return [it["text"] for it in items]


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


def main() -> int:
    _load_dotenv(_ENV_FILE)

    base_url = os.environ.get("TEST_BASE_URL", "http://127.0.0.1:9001/v1")
    api_key = os.environ.get("TEST_APIKEY", "")
    model = os.environ.get("TEST_MODEL_NAME", "")

    if not api_key or not model:
        print("缺少配置：TEST_APIKEY / TEST_MODEL_NAME")
        print(f"请在 {_ENV_FILE} 填入（或用环境变量）：")
        print("    TEST_APIKEY=sk-xxxxxxxx")
        print("    TEST_MODEL_NAME=deepseek-chat")
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
                with client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": p}],
                    stream=True,
                ) as s:
                    for c in s:
                        if c.choices and c.choices[0].delta.content:
                            chunks.append(c.choices[0].delta.content)
                reply = "".join(chunks)
                mode = "流式"
            else:
                resp = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": p}],
                )
                reply = resp.choices[0].message.content or ""
                mode = "非流式"

            ok += 1
            print(f"[{i:2d}/{n}] {mode} ✓ {time.time()-t0:5.1f}s | {p[:18]}... → {reply[:40]!r}")
        except Exception as e:
            err += 1
            print(f"[{i:2d}/{n}] ✗ {time.time()-t0:5.1f}s | {p[:18]}... → 错误: {str(e)[:80]}")

    print("=" * 70)
    print(f"完成: 成功 {ok} / 失败 {err}（失败请求同样会被 ssgc 记录并上报）")
    print("下一步: 等上报周期（默认 60s）后查 mock http://127.0.0.1:8000/records 或 ssgc report")
    return 0


if __name__ == "__main__":
    sys.exit(main())

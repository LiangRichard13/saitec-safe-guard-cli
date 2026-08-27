"""手动联调脚本：经 ssgc 代理验证 anthropic-messages 协议全链路

不是 pytest 测试（命名避开 test_ 前缀，pytest 不会收集）。用官方 anthropic SDK
走真实客户端行为（自带 x-api-key / anthropic-version 头 + SSE 流式），与真实
Claude Code 的调用形态一致——历史 gzip 透传 bug 正是这类真实客户端暴露的。

运行：

    cd saitec-safe-guard-cli
    python tests/test_chat/probe_anthropic_messages.py [N]

配置（优先级：环境变量 > test_chat/.env > 默认值，三协议探针共享同一组变量）：

    TEST_BASE_URL      本地代理地址。anthropic SDK 只需 host（自拼 /v1/messages），
                       如 http://127.0.0.1:9002 —— 注意与 openai 系探针不同，不以 /v1 结尾
    TEST_APIKEY        模型厂商的真实 API key（必填）
    TEST_MODEL_NAME    模型名（必填；DeepSeek /anthropic 口用 deepseek-chat 等）

依赖：pip install anthropic（手动联调依赖，不在 pyproject 内）
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from anthropic import Anthropic

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

    base_url = os.environ.get("TEST_BASE_URL", "http://127.0.0.1:9002")
    api_key = os.environ.get("TEST_APIKEY", "")
    model = os.environ.get("TEST_MODEL_NAME", "")

    if not api_key or not model:
        print("缺少配置：TEST_APIKEY / TEST_MODEL_NAME")
        print(f"请在 {_ENV_FILE} 填入（或用环境变量）：")
        print("    TEST_APIKEY=sk-xxxxxxxx")
        print("    TEST_MODEL_NAME=deepseek-chat   # DeepSeek /anthropic 口模型名")
        return 1

    prompts_all = _load_prompts()
    n = int(sys.argv[1]) if len(sys.argv) > 1 else len(prompts_all)
    prompts = (prompts_all * 10)[:n]

    client = Anthropic(base_url=base_url, api_key=api_key)
    print(f"目标: {base_url}  模型: {model}  key: {api_key[:8]}***")
    print("=" * 70)

    ok = err = 0
    for i, p in enumerate(prompts, 1):
        t0 = time.time()
        try:
            stream = i > n - 2  # 最后 2 条流式（覆盖 SSE 路径），其余非流式
            max_tokens = 256
            if stream:
                with client.messages.stream(
                    model=model,
                    max_tokens=max_tokens,
                    messages=[{"role": "user", "content": p}],
                ) as s:
                    chunks: list[str] = []
                    for text in s.text_stream:
                        chunks.append(text)
                reply = "".join(chunks)
                mode = "流式"
            else:
                resp = client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    messages=[{"role": "user", "content": p}],
                )
                # content 是 block 数组（text blocks），拼出纯文本
                reply = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
                mode = "非流式"

            ok += 1
            print(f"[{i:2d}/{n}] {mode} ✓ {time.time()-t0:5.1f}s | {p[:18]}... → {reply[:40]!r}")
        except Exception as e:
            err += 1
            print(f"[{i:2d}/{n}] ✗ {time.time()-t0:5.1f}s | {p[:18]}... → 错误: {str(e)[:90]}")

    print("=" * 70)
    print(f"完成: 成功 {ok} / 失败 {err}")
    print("下一步: 等 上报周期后查 mock http://127.0.0.1:8000/records 或 ssgc report --json")
    print("排查: 客户端解码类报错先看 JSONL 该条 response.content 是否完整（代理侧证据）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""export — 导出检测报告（Markdown / HTML）

从 SQLite 查结论、从 JSONL 关联原始对话，双格式同源渲染：

    ssgc export                          # md，默认只导 suspicious/violation/error
    ssgc export --status all             # 全量（含 clean）
    ssgc export -f html -o report.html   # HTML 报告

设计约定：
- 结论 + 完整对话（JSONL 缺失时该条标注"仅结论"，不报错）
- 默认过滤异常是产品决策：clean 通常占 95%+，会稀释重点；存档场景显式 --status all
"""
from __future__ import annotations

import asyncio
import html as html_mod
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import typer

from .._common import CHART, EXIT_USER_ERROR, console, emit, get_config_path
from ...core.models import Record
from ...store.store import Store
from .report import _parse_since

VALID_STATUSES = ("clean", "suspicious", "violation", "error")
DEFAULT_STATUSES = ["suspicious", "violation", "error"]


# ============================================================
# 参数解析辅助
# ============================================================


def _parse_status(raw: str | None) -> list[str]:
    """解析 --status：逗号分隔；'all' 展开为四值全量；None 用默认（仅异常）"""
    if raw is None:
        return list(DEFAULT_STATUSES)
    parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
    if not parts:
        raise typer.BadParameter(f"--status 不能为空（可选: {', '.join(VALID_STATUSES)} 或 all）")
    if parts == ["all"]:
        return list(VALID_STATUSES)
    bad = [p for p in parts if p not in VALID_STATUSES]
    if bad:
        raise typer.BadParameter(
            f"未知的 detection_status: {', '.join(bad)}（可选: {', '.join(VALID_STATUSES)} 或 all）"
        )
    return parts


def _load_records_bulk(records_dir: Path, wanted: set[str]) -> dict[str, Record]:
    """单次遍历 records-*.jsonl 收集目标记录（关联原始对话）"""
    found: dict[str, Record] = {}
    if not wanted or not records_dir.exists():
        return found
    for f in sorted(records_dir.glob("records-*.jsonl")):
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            rid = d.get("record_id")
            if rid in wanted and rid not in found:
                found[rid] = Record(
                    record_id=rid,
                    service=d.get("service", ""),
                    endpoint_type=d.get("endpoint_type", ""),
                    upstream=d.get("upstream", ""),
                    path=d.get("path", ""),
                    timestamp=d.get("timestamp", ""),
                    elapsed_ms=d.get("elapsed_ms", 0),
                    status_code=d.get("status_code", 0),
                    error=d.get("error"),
                    request=d.get("request") or {},
                    response=d.get("response") or {},
                )
    return found


def _flatten_content(content: object) -> str:
    """把 message/response 的 content 归一成纯文本

    str 原样；Anthropic 多段 list 取 text 段拼接，非文本段序列化占位；
    其他类型 json 序列化兜底。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for seg in content:
            if isinstance(seg, dict):
                if seg.get("type") == "text":
                    parts.append(str(seg.get("text", "")))
                else:
                    parts.append(f"[{seg.get('type', 'unknown')} 段]")
            else:
                parts.append(str(seg))
        return "\n".join(parts)
    return json.dumps(content, ensure_ascii=False)


def _extract_dialogue(record: Record | None) -> tuple[list[dict[str, str]] | None, str | None]:
    """从 Record 提取对话：(messages, reply)。Record 为 None 表示 JSONL 缺失。"""
    if record is None:
        return None, None
    req = record.request or {}
    resp = record.response or {}
    raw_msgs = req.get("messages")
    messages: list[dict[str, str]] | None = None
    if isinstance(raw_msgs, list) and raw_msgs:
        messages = [
            {"role": str(m.get("role", "?")), "content": _flatten_content(m.get("content"))}
            for m in raw_msgs
            if isinstance(m, dict)
        ]
    reply_raw = resp.get("content")
    reply: str | None = _flatten_content(reply_raw) if reply_raw is not None else None
    return messages, reply


def _collect_rows(
    db_path: Path,
    records_dir: Path,
    since: datetime,
    service: str | None,
    limit: int,
    statuses: list[str],
) -> dict:
    async def _q() -> dict:
        store = Store(db_path)
        results = await store.query(since, service=service, limit=limit, status=statuses)
        by_id = {r.record_id: r for r in results}
        records = _load_records_bulk(records_dir, set(by_id))
        rows = []
        for r in results:
            rec = records.get(r.record_id)
            messages, reply = _extract_dialogue(rec)
            rows.append({
                "record_id": r.record_id,
                "service": r.service,
                "endpoint_type": r.endpoint_type,
                "upstream": r.upstream,
                "path": rec.path if rec else "-",
                "model": r.model,
                "timestamp": r.timestamp,
                "status_code": r.status_code,
                "elapsed_ms": r.elapsed_ms,
                "finish_reason": r.finish_reason,
                "prompt_tokens": r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
                "detected_at": r.detected_at,
                "detection_status": r.detection_status,
                "risk_level": r.risk_level,
                "detail": r.detection_detail,
                "error": r.error,
                "has_original": rec is not None,
                "messages": messages,
                "reply": reply,
            })
        return {
            "rows": rows,
            "truncated": len(results) >= limit,
        }

    return asyncio.run(_q())


def _build_report_data(rows: list[dict], truncated: bool, since_dt: datetime,
                       statuses: list[str], service: str | None) -> dict:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    by_status = dict(Counter(r["detection_status"] for r in rows))
    return {
        "generated_at": now,
        "since": since_dt.isoformat(timespec="seconds"),
        "until": now,
        "statuses": statuses,
        "service": service,
        "total": len(rows),
        "truncated": truncated,
        "by_status": by_status,
        "rows": rows,
    }


def _detail_reason(detail: object) -> str:
    if isinstance(detail, dict):
        return str(detail.get("reason") or "")
    return ""


def _to_local(iso: object) -> str:
    """ISO8601 时间转本地时区 'YYYY-MM-DD HH:MM:SS'。

    报告面向人类，显示本地时间而非 UTC。容忍：Z/+00:00 后缀、naive（缺时区当 UTC，
    detector 可能不写时区）、空值、解析失败（原样返回）。
    """
    if not iso:
        return "-"
    s = str(iso)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return s
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
# Markdown 渲染
# ============================================================


def _render_markdown(data: dict) -> str:
    L: list[str] = []
    L.append("# SSGC 检测报告\n")
    L.append(f"- **生成时间**: {_to_local(data['generated_at'])}")
    L.append(f"- **数据窗口**: {_to_local(data['since'])} 起")
    L.append(f"- **结论过滤**: {'、'.join(data['statuses'])}"
             + ("" if set(data["statuses"]) == set(VALID_STATUSES) else "（不含 clean；全量请 --status all）"))
    if data["service"]:
        L.append(f"- **服务过滤**: {data['service']}")
    L.append(f"- **导出条数**: {data['total']}" + ("（达到上限截断）" if data["truncated"] else ""))
    dist = " · ".join(f"{k} {v}" for k, v in sorted(data["by_status"].items()))
    if dist:
        L.append(f"- **状态分布**: {dist}")
    L.append("")
    L.append("> ⚠️ 本报告含完整对话内容（敏感），请注意存放与分享范围。\n")

    if not data["rows"]:
        L.append("_时间窗口内没有符合过滤条件的检测记录。_")
        return "\n".join(L)

    L.append("## 汇总\n")
    L.append("| # | 时间（本地） | 服务 | 上游 | 模型 | 结论 | 风险 | 理由 | 记录 ID |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(data["rows"], 1):
        ts = _to_local(r["timestamp"])
        reason = _detail_reason(r["detail"]).replace("|", "\\|")
        upstream_short = (r["upstream"] or "").replace("https://", "").replace("http://", "")[:30]
        L.append(
            f"| {i} | {ts} | {r['service']} | {upstream_short} | {r['model'] or '-'} "
            f"| {r['detection_status']} | {r['risk_level'] or '-'} | {reason} "
            f"| `{r['record_id'][:8]}` |"
        )
    L.append("")

    L.append("## 记录详情\n")
    for i, r in enumerate(data["rows"], 1):
        L.append(f"### [{i}] {r['detection_status']}"
                 + (f" / {r['risk_level']}" if r["risk_level"] else "")
                 + f" · `{r['record_id']}`\n")
        L.append("| 字段 | 值 |")
        L.append("|---|---|")
        L.append(f"| 时间 | {_to_local(r['timestamp'])} |")
        L.append(f"| 服务 / 模型 | {r['service']} / {r['model'] or '-'} |")
        L.append(f"| 上游端点 | {r['upstream']} |")
        L.append(f"| 协议 / 路径 | {r['endpoint_type']} / {r['path']} |")
        toks = "-"
        if r["prompt_tokens"] is not None or r["completion_tokens"] is not None:
            toks = f"{r['prompt_tokens'] or 0} + {r['completion_tokens'] or 0}"
        L.append(f"| tokens (p/c) | {toks} |")
        L.append(f"| 上游耗时 / 状态码 | {r['elapsed_ms']}ms / {r['status_code']} |")
        L.append(f"| 结束原因 | {r['finish_reason'] or '-'} |")
        L.append(f"| 检测完成时间 | {_to_local(r['detected_at'])} |")
        if r.get("error"):
            L.append(f"| 代理错误 | ⚠️ {r['error']} |")
        L.append("")
        if r["detail"]:
            L.append("**detection_detail**:\n")
            L.append("```json")
            L.append(json.dumps(r["detail"], ensure_ascii=False, indent=2))
            L.append("```\n")
        if not r["has_original"]:
            L.append("> ⚠️ 原始内容缺失（JSONL 已清理或非本机记录），以上仅有检测结论。\n")
            continue
        msgs = r["messages"]
        if msgs:
            L.append("**对话**:\n")
            for m in msgs:
                L.append(f"**[{m['role']}]**\n")
                L.append("```text")
                L.append(str(m["content"]) or "(空)")
                L.append("```\n")
        reply = r["reply"]
        if reply is not None:
            L.append("**[assistant 回复]**\n")
            L.append("```text")
            L.append(str(reply) or "(空)")
            L.append("```\n")
    return "\n".join(L)


# ============================================================
# HTML 渲染
# ============================================================

_STATUS_COLOR = {
    "violation": "#d92d20",
    "suspicious": "#b54708",
    "clean": "#12805c",
    "error": "#667085",
}

_CSS = """
:root{color-scheme:light}
*{box-sizing:border-box}
body{margin:0;background:#f6f7f9;color:#1a1f27;font-family:-apple-system,'Segoe UI','Microsoft YaHei','PingFang SC',sans-serif;font-size:14px;line-height:1.6}
.wrap{max-width:960px;margin:0 auto;padding:32px 20px 64px}
header h1{font-size:24px;margin:0 0 4px}
header .meta{color:#6b7280;font-size:12.5px;margin-bottom:16px}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:20px}
.card{background:#fff;border:1px solid #e4e7ec;border-radius:10px;padding:12px 18px;min-width:110px}
.card .num{font-size:22px;font-weight:700;line-height:1.2}
.card .lbl{font-size:12px;color:#6b7280}
.note{background:#fffaeb;border:1px solid #fedf89;border-radius:8px;padding:8px 14px;font-size:13px;color:#7a2e0e;margin-bottom:24px}
section.summary h2,details.rec h2,h2{font-size:15px}
table{border-collapse:collapse;width:100%;background:#fff;border:1px solid #e4e7ec;border-radius:10px;overflow:hidden;font-size:13px}
th{background:#f9fafb;text-align:left;padding:8px 10px;border-bottom:1px solid #e4e7ec;font-weight:600;white-space:nowrap}
td{padding:7px 10px;border-bottom:1px solid #f2f4f7;vertical-align:top}
tr:last-child td{border-bottom:none}
tbody tr:hover{background:#f9fafb}
.badge{display:inline-block;padding:1px 9px;border-radius:99px;color:#fff;font-size:12px;font-weight:600;white-space:nowrap}
.risk{font-weight:600}
details.rec{background:#fff;border:1px solid #e4e7ec;border-left-width:4px;border-radius:10px;margin-top:14px;padding:0}
details.rec>summary{cursor:pointer;padding:12px 16px;display:flex;align-items:center;gap:10px;list-style:none;flex-wrap:wrap}
details.rec>summary::-webkit-details-marker{display:none}
details.rec>summary .tid{color:#98a2b3;font-size:12px;font-family:ui-monospace,Consolas,monospace}
details.rec>summary .spacer{flex:1}
details.rec .body{padding:0 16px 16px;border-top:1px dashed #eaecf0}
.meta-grid{display:grid;grid-template-columns:auto 1fr auto 1fr;gap:4px 12px;font-size:12.5px;margin:12px 0;color:#475467}
.meta-grid b{color:#1a1f27;font-weight:600}
h3.sec{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:#98a2b3;margin:14px 0 6px}
pre.detail-json{background:#f9fafb;border:1px solid #eaecf0;border-radius:8px;padding:10px 12px;font-size:12px;font-family:ui-monospace,Consolas,monospace;overflow-x:auto;white-space:pre-wrap;word-break:break-word}
.msg{border-left:3px solid #98a2b3;background:#fcfcfd;border-radius:0 8px 8px 0;padding:8px 12px;margin:8px 0}
.msg.user{border-color:#175cd3;background:#f5f8ff}
.msg.assistant{border-color:#6941c6;background:#faf9ff}
.msg .who{font-size:11.5px;font-weight:700;color:#475467;text-transform:uppercase;letter-spacing:.04em;margin-bottom:2px}
.msg pre{margin:0;font-family:inherit;white-space:pre-wrap;word-break:break-word;font-size:13px}
.missing{color:#b54708;font-size:13px;background:#fffaeb;border-radius:8px;padding:8px 12px}
.empty{color:#667085;font-style:italic}
footer{margin-top:36px;color:#98a2b3;font-size:12px;text-align:center}
@media print{body{background:#fff}.wrap{max-width:none;padding:0}details.rec{break-inside:avoid}}
"""


def _esc(s: object) -> str:
    return html_mod.escape(str(s), quote=True)


def _html_messages_block(r: dict) -> str:
    if not r["has_original"]:
        return '<div class="missing">⚠️ 原始内容缺失（JSONL 已清理或非本机记录），此条仅有检测结论。</div>'
    out: list[str] = []
    msgs = r["messages"]
    if msgs:
        out.append('<div class="msgs">')
        for m in msgs:
            role = html_mod.escape(m["role"])
            cls = "msg user" if role == "user" else ("msg assistant" if role == "assistant" else "msg")
            out.append(f'<div class="{cls}"><div class="who">{role}</div><pre>{_esc(m["content"])}</pre></div>')
        out.append("</div>")
    if r["reply"] is not None:
        shown = r["reply"] or "(空)"
        out.append(f'<div class="msg assistant"><div class="who">assistant 回复</div><pre>{_esc(shown)}</pre></div>')
    if not out:
        return '<div class="empty">(该记录不含可展示的对话内容)</div>'
    return "\n".join(out)


def _render_html(data: dict) -> str:
    full_export = set(data["statuses"]) == set(VALID_STATUSES)
    badges = "".join(
        f'<span class="badge" style="background:{_STATUS_COLOR[k]}">{_esc(k)} {_esc(v)}</span>'
        for k, v in sorted(data["by_status"].items())
    )
    filters_txt = "、".join(data["statuses"]) + (
        "" if full_export else "（不含 clean）"
    ) + (f" · 服务 { _esc(data['service']) }" if data["service"] else "")
    trunc_note = " · <b style='color:#b54708'>已达上限截断</b>" if data["truncated"] else ""

    recs: list[str] = []
    for i, r in enumerate(data["rows"], 1):
        st = r["detection_status"]
        color = _STATUS_COLOR.get(st, "#667085")
        reason = _detail_reason(r["detail"])
        # 折叠策略：全量导出时 clean/error 收起（首屏聚焦异常）；默认异常导出全开
        open_attr = "" if (full_export and st in ("clean", "error")) else " open"
        toks = "-"
        if r["prompt_tokens"] is not None or r["completion_tokens"] is not None:
            toks = f"{r['prompt_tokens'] or 0} + {r['completion_tokens'] or 0}"
        detail_html = ""
        if r["detail"]:
            detail_json = json.dumps(r["detail"], ensure_ascii=False, indent=2)
            detail_html = f'<h3 class="sec">检测明细</h3><pre class="detail-json">{_esc(detail_json)}</pre>'
        recs.append(f"""
<details class="rec" style="border-left-color:{color}"{open_attr}>
<summary>
  <span class="badge" style="background:{color}">{_esc(st)}</span>
  <span class="risk">{_esc(r["risk_level"] or "-")}</span>
  <b>{i:03d}</b>
  <span>{_esc(_to_local(r["timestamp"]))}</span>
  <span>{_esc(r["model"] or "-")}</span>
  {f'<span title="{_esc(reason)}">{_esc(reason)}</span>' if reason else ""}
  <span class="spacer"></span>
  <span class="tid">{_esc(r["record_id"][:8])}</span>
</summary>
<div class="body">
<div class="meta-grid">
<b>记录 ID</b><span style="font-family:ui-monospace,Consolas,monospace">{_esc(r["record_id"])}</span>
<b>服务</b><span>{_esc(r["service"])}</span>
<b>上游端点</b><span>{_esc(r["upstream"])}</span>
<b>协议 / 路径</b><span>{_esc(r["endpoint_type"])} / {_esc(r["path"])}</span>
<b>时间（本地）</b><span>{_esc(_to_local(r["timestamp"]))}</span>
<b>检测完成</b><span>{_esc(_to_local(r["detected_at"]))}</span>
<b>tokens (p+c)</b><span>{toks}</span>
<b>耗时 / HTTP</b><span>{r["elapsed_ms"]}ms / {r["status_code"]}</span>
<b>结束原因</b><span>{_esc(r["finish_reason"] or "-")}</span>
{"<b>代理错误</b><span style='color:#b54708'>⚠️ " + _esc(r["error"]) + "</span>" if r.get("error") else ""}
</div>
{detail_html}
<h3 class="sec">对话内容</h3>
{_html_messages_block(r)}
</div>
</details>""")

    rows_html = "\n".join(recs)
    empty_note = "" if data["rows"] else '<p class="empty">时间窗口内没有符合过滤条件的检测记录。</p>'
    table_rows = "".join(
        f"<tr><td>{i}</td>"
        f"<td>{_esc(_to_local(r['timestamp']))}</td>"
        f"<td>{_esc(r['service'])}</td>"
        f"<td>{_esc((r['upstream'] or '').replace('https://', '').replace('http://', '')[:30])}</td>"
        f"<td>{_esc(r['model'] or '-')}</td>"
        f"<td><span class='badge' style='background:{_STATUS_COLOR.get(r['detection_status'], '#667085')}'>{_esc(r['detection_status'])}</span></td>"
        f"<td class='risk'>{_esc(r['risk_level'] or '-')}</td>"
        f"<td>{_esc(_detail_reason(r['detail']))}</td>"
        f"<td style='font-family:ui-monospace,Consolas,monospace'>{_esc(r['record_id'][:8])}</td></tr>"
        for i, r in enumerate(data["rows"], 1)
    )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SSGC 检测报告 · {_to_local(data["generated_at"])[:10]}</title>
<style>{_CSS}</style>
<script>
window.onbeforeprint=function(){{document.querySelectorAll('details').forEach(function(d){{d.open=true}})}};
</script>
</head>
<body>
<div class="wrap">
<header>
<h1>🛡️ SSGC 检测报告</h1>
<div class="meta">生成 {_to_local(data["generated_at"])} · 数据自 {_to_local(data["since"])} · 结论过滤: {filters_txt}{trunc_note}</div>
</header>
<div class="cards">
<div class="card"><div class="num">{data["total"]}</div><div class="lbl">导出条数</div></div>
{"".join(f'<div class="card"><div class="num" style="color:{_STATUS_COLOR.get(k, "#667085")}">{v}</div><div class="lbl">{_esc(k)}</div></div>' for k, v in sorted(data["by_status"].items()))}
</div>
<div class="note">⚠️ 本报告含完整对话内容（敏感），请注意存放与分享范围。</div>
{empty_note}
<section class="summary">
<h2>汇总</h2>
<table><thead><tr><th>#</th><th>时间（本地）</th><th>服务</th><th>上游</th><th>模型</th><th>结论</th><th>风险</th><th>理由</th><th>ID</th></tr></thead>
<tbody>
{table_rows}
</tbody></table>
</section>
<h2 style="margin-top:28px">记录详情（点击行展开/收起）</h2>
{rows_html}
<footer>由 ssgc export 生成 · saitec-safe-guard-cli</footer>
</div>
</body>
</html>"""


# ============================================================
# CLI 入口
# ============================================================


def do_export(
    ctx: typer.Context,
    config_path: Path | None = typer.Option(None, "--config", "-c"),
    json_output: bool = typer.Option(False, "--json"),
    fmt: str = typer.Option("md", "--format", "-f", help="输出格式: md 或 html"),
    output: Path | None = typer.Option(None, "--output", "-o", help="输出文件路径（缺省 ssgc-report-<时间戳>.<ext>）"),
    since: str | None = typer.Option(None, "--since", help="起始时间（ISO8601 或 1h/30m/7d，默认 7d）"),
    status: str | None = typer.Option(
        None, "--status",
        help=f"逗号分隔的结论过滤（默认 {','.join(DEFAULT_STATUSES)}；'all'=全量含 clean）",
    ),
    service: str | None = typer.Option(None, "--service", "-s", help="按服务名过滤"),
    limit: int = typer.Option(10000, "--limit", "-n", help="导出条数上限"),
) -> None:
    """📄 导出检测报告（Markdown / HTML，含完整对话）

    SQLite 检测结论 + JSONL 原始对话 join 后输出可读报告。HTML 自包含
    （单文件可直接浏览器打开，含异常展开 / 打印友好样式）。

    默认只导 suspicious / violation / error（clean 占 95%+ 会稀释重点），
    全量导出显式 `--status all`。

    \b
    Examples:
      ssgc export                            # 默认 md，7 天异常
      ssgc export --format html              # HTML 自包含报告
      ssgc export --since 1d --status all    # 1 天全量
      ssgc export --status violation --service deepseek-claude
      ssgc export -o report.html --format html   # 指定输出路径

    \b
    Troubleshooting:
      • NO_DB → 尚无上报（等一个 report_interval）
      • JSONL 缺失 → 报告里标注"JSONL 缺失，仅显示结论"（可能 purge 清了）
      • 导出为空 → 检查 `--since` 时间窗与 `--status` 过滤

    \b
    See also:
      `ssgc report` 实时查询    `ssgc redo` 重报单条
    """
    fmt_l = fmt.strip().lower()
    if fmt_l not in ("md", "html"):
        emit(json_output=json_output, ok=False,
             error={"code": "BAD_FORMAT", "message": f"--format 只支持 md/html，收到: {fmt}"},
             exit_code=EXIT_USER_ERROR)
        return

    path = config_path.expanduser().resolve() if config_path else get_config_path(ctx)
    db_path = path.parent / "results.db"
    if not db_path.exists():
        emit(json_output=json_output, ok=False,
             error={"code": "NO_DB", "message": f"检测结果库不存在: {db_path}（尚无上报）"},
             exit_code=EXIT_USER_ERROR)
        return

    try:
        statuses = _parse_status(status)
    except typer.BadParameter as e:
        emit(json_output=json_output, ok=False,
             error={"code": "BAD_STATUS", "message": str(e)},
             exit_code=EXIT_USER_ERROR)
        return

    since_dt = _parse_since(since) if since else datetime.now(timezone.utc) - timedelta(days=7)

    try:
        collected = _collect_rows(db_path, path.parent / "records", since_dt, service, limit, statuses)
        data = _build_report_data(collected["rows"], collected["truncated"], since_dt, statuses, service)
        rendered = _render_markdown(data) if fmt_l == "md" else _render_html(data)
    except Exception as e:
        emit(json_output=json_output, ok=False,
             error={"code": "EXPORT_ERROR", "message": str(e)})
        return

    out_path: Path
    if output is not None:
        out_path = output.expanduser()
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        out_path = Path.cwd() / f"ssgc-report-{stamp}.{fmt_l}"

    try:
        out_path.write_text(rendered, encoding="utf-8")
    except OSError as e:
        emit(json_output=json_output, ok=False,
             error={"code": "WRITE_ERROR", "message": f"写入失败: {e}"},
             exit_code=EXIT_USER_ERROR)
        return

    flagged = sum(v for k, v in data["by_status"].items() if k in ("violation", "suspicious"))
    if not json_output:
        console.print(f"{CHART} [green]已导出 {data['total']} 条[/green] → [cyan]{out_path}[/cyan]"
                      + ("[yellow]（已达上限截断）[/yellow]" if data["truncated"] else ""))
    emit(
        json_output=json_output,
        data={
            "count": data["total"],
            "format": fmt_l,
            "output_path": str(out_path),
            "by_status": data["by_status"],
            "flagged": flagged,
            "truncated": data["truncated"],
        },
    )

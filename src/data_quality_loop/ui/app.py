# -*- coding: utf-8 -*-
"""
DQL 看板 UI — Gradio 界面（人类友好），调用 FastAPI 接入层

能力:
  - 概览:  数仓表清单 + 各表异常数
  - 异常:  当前异常清单(可刷新)
  - 修复:  对指定表触发一轮质检循环(Fixer→Verifier→Orchestrator)
  - 报告:  查看某表的修复记录 + 审计日志
  - 升级:  未处理的升级清单

运行:
  本地:  DQL_API_URL=http://localhost:8500 API_TOKEN=... python -m data_quality_loop.ui.app
  Docker: docker compose up -d ui  (端口 8600)
"""

import json
import os

import gradio as gr
import requests

API_URL = os.getenv("DQL_API_URL", "http://localhost:8500")
TOKEN = os.getenv("API_TOKEN", "")


def _headers() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}


def _req(method: str, path: str, body=None, timeout: int = 300):
    try:
        r = requests.request(
            method, f"{API_URL}{path}", headers=_headers(), json=body, timeout=timeout
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


# ═══════════════════════════════════════
# 数据获取
# ═══════════════════════════════════════


def overview_text() -> str:
    tables = _req("GET", "/api/tables").get("tables", [])
    ans = _req("GET", "/api/anomalies")
    anomalies = ans.get("anomalies", [])
    per = {}
    for a in anomalies:
        per[a.get("table", "?")] = per.get(a.get("table", "?"), 0) + 1
    lines = [f"📊 数仓共 **{len(tables)}** 张表,当前 **{len(anomalies)}** 个异常\n"]
    for t in tables:
        lines.append(f"  - **{t}**: {per.get(t, 0)} 个异常")
    if ans.get("error"):
        lines.append(f"\n⚠️ API 连接错误: {ans['error']}")
    return "\n".join(lines)


def anomalies_rows():
    d = _req("GET", "/api/anomalies")
    if d.get("error"):
        return [["连接失败", d["error"], ""]]
    rows = [
        [
            a.get("table", ""),
            a.get("rule", ""),
            str(a.get("evidence_rows") or a.get("rows_affected") or a.get("count") or "-"),
        ]
        for a in d.get("anomalies", [])
    ]
    return rows if rows else [["(无异常)", "", ""]]


def fix_table(table: str, max_rounds: int = 3) -> str:
    if not table:
        return "请先选择表"
    d = _req("POST", f"/api/fix/{table}", {"max_rounds": max_rounds})
    if d.get("error"):
        return f"❌ {d['error']}"
    icon = "✅" if d.get("status") == "completed" else "🆘"
    return f"{icon} 表 **{table}** → **{d.get('status')}**(轮数 {d.get('rounds')})\n\n重新扫描看看收敛结果。"


def report_md(table: str) -> str:
    if not table:
        return "请先选择表"
    d = _req("GET", f"/api/report/{table}")
    if d.get("error"):
        return f"❌ {d['error']}"
    lines = [f"## 📋 表 {table} 质检报告\n"]
    fr = d.get("fix_records", [])
    lines.append(f"**修复记录** ({len(fr)} 条)")
    for r in fr:
        lines.append(f"- {r.get('ts','')[:19]} | {r.get('anomaly_key')} | 影响 {r.get('rows_affected')} 行 | {r.get('status')}")
    aud = d.get("audit", [])
    lines.append(f"\n**审计日志** ({len(aud)} 条)")
    for a in aud[:15]:
        detail = (a.get("detail") or "")[:80]
        lines.append(f"- {a.get('ts','')[:19]} | {a.get('action')} | {detail}")
    return "\n".join(lines)


def escalations_rows():
    """升级清单 — /api/escalations 返回可能是 dict 或 list，需兼容"""
    d = _req("GET", "/api/escalations")
    if isinstance(d, dict):
        if d.get("error"):
            return [["连接失败", d["error"]]]
        items = d.get("escalations", [])
    else:
        items = d if isinstance(d, list) else []
    rows = [
        [e.get("table", ""), (e.get("ts", "")[:19]), (e.get("detail") or "")[:60]]
        for e in items
    ]
    return rows if rows else [["(无升级)", "", ""]]


# ═══════════════════════════════════════
# 界面
# ═══════════════════════════════════════


def build_app() -> gr.Blocks:
    with gr.Blocks(title="Data Quality Loop 看板") as demo:
        gr.Markdown(
            "# 🔄 Data Quality Loop — 数据质量巡检循环看板\n"
            "**扫描异常 → Fixer 生成方案 → Verifier 副本验证 → Orchestrator 落库 → 收敛或升级**"
        )

        # ── 概览 ──
        with gr.Tab("📊 概览"):
            overview_out = gr.Markdown("加载中...")
            with gr.Row():
                gr.Button("刷新概览").click(overview_text, outputs=overview_out)
            overview_out.value = overview_text()

        # ── 异常清单 ──
        with gr.Tab("⚠️ 异常清单"):
            anom_df = gr.Dataframe(
                headers=["表", "规则", "行数"], datatype=["str", "str", "str"], interactive=False
            )
            gr.Button("🔄 刷新异常").click(
                lambda: anomalies_rows(), outputs=anom_df
            )
            anom_df.value = anomalies_rows()

        # ── 修复 ──
        with gr.Tab("🔧 修复"):
            with gr.Row():
                fix_table_dd = gr.Dropdown(choices=[], label="选择表")
                fix_rounds = gr.Slider(1, 5, value=3, step=1, label="最大轮数")
            fix_btn = gr.Button("▶️ 触发质检循环(可能耗时)")
            fix_out = gr.Markdown("选择表后点击触发")
            fix_btn.click(fix_table, inputs=[fix_table_dd, fix_rounds], outputs=fix_out)

            def _load_tables():
                return _req("GET", "/api/tables").get("tables", [])
            gr.Button("加载表列表").click(lambda: gr.Dropdown(choices=_load_tables()), outputs=fix_table_dd)

        # ── 报告 ──
        with gr.Tab("📋 报告"):
            rep_table_dd = gr.Dropdown(choices=[], label="选择表")
            rep_btn = gr.Button("查看报告")
            rep_out = gr.Markdown("选择表后查看")
            rep_btn.click(report_md, inputs=[rep_table_dd], outputs=rep_out)
            gr.Button("加载表列表").click(lambda: gr.Dropdown(choices=_load_tables()), outputs=rep_table_dd)

        # ── 升级 ──
        with gr.Tab("🆘 升级清单"):
            esc_df = gr.Dataframe(
                headers=["表", "时间", "说明"], datatype=["str", "str", "str"], interactive=False
            )
            gr.Button("🔄 刷新升级").click(lambda: escalations_rows(), outputs=esc_df)
            esc_df.value = escalations_rows()

    return demo


app = build_app()

if __name__ == "__main__":
    port = int(os.getenv("DQL_UI_PORT", "8600"))
    app.launch(server_name="0.0.0.0", server_port=port)

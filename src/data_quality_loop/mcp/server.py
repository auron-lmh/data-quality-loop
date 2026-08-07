# -*- coding: utf-8 -*-
"""MCP Server：把数据质量巡检能力暴露为 MCP 工具

“数据治理能力标准化输出” —— 供 Claude Desktop / Claude Code /
企业 LLM 平台 / 其他 MCP Client 直接调用质检循环。

运行（stdio transport）:
    python -m data_quality_loop.mcp        # 需 PYTHONPATH=src

暴露工具:
  - list_quality_tables()   配置了质量规则的表
  - scan_anomalies()        只读扫描当前数仓异常清单
  - run_quality_check(table) 对表跑完整质检循环(自动修复/收敛/升级)
  - get_quality_report(table) 质检报告(修复记录 + 审计)

复用同一核心循环(DataQualityLoop),不新增业务逻辑。
"""
import os, sys
import anyio
from mcp.server.fastmcp import FastMCP

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

from data_quality_loop import audit
from data_quality_loop.quality_rules import scan_all
from data_quality_loop.data_quality_loop import con, _rules, _DB_LOCK, DataQualityLoop

server = FastMCP(
    "data-quality-loop",
    instructions="数据质量巡检循环：扫描数仓异常 → 自动修复(副本验证) → 收敛或升级人工。Loop Engineering + Maker-Checker。",
)

_loop = None


def _get_loop() -> DataQualityLoop:
    global _loop
    if _loop is None:
        _loop = DataQualityLoop()               # 懒加载:构建编排者较慢
    return _loop


@server.tool()
async def list_quality_tables() -> list:
    """列出配置了质量规则的表。"""
    return list(_rules.keys())


@server.tool()
async def scan_anomalies() -> dict:
    """只读扫描当前数仓的异常清单(空值/主键重复/引用悬空/日期格式/勾稽/枚举)。"""
    with _DB_LOCK:
        ans = [a.to_dict() for a in scan_all(con, _rules)]
    return {"count": len(ans), "anomalies": ans}


@server.tool()
async def run_quality_check(table: str) -> dict:
    """对指定表跑一轮完整质检循环：Fixer 生成修复 → Verifier 副本验证 → 落库/升级。返回处理结果。"""
    if table not in _rules:
        return {"error": f"表 {table} 未配置质量规则", "tables": list(_rules.keys())}
    result = await anyio.to_thread.run_sync(
        lambda: _get_loop().process_one(table, 3))
    return result


@server.tool()
async def get_quality_report(table: str) -> dict:
    """某表质检报告：修复记录 + 审计日志 + 循环状态。"""
    return audit.get_report(table)

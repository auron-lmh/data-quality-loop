# -*- coding: utf-8 -*-
"""
SQLite 审计层 —— 修复记录 + 审计日志 + 循环状态,跨会话持久化
(决策 4:量级年几十万行远低于 SQLite 上限,零依赖单文件,标准 SQL 可查询)
"""
import os
import sqlite3
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AUDIT_DB = os.path.join(ROOT, "data", "quality_audit.db")

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, table_name TEXT, action TEXT, detail TEXT)""",
    """CREATE TABLE IF NOT EXISTS fix_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, table_name TEXT, anomaly_key TEXT,
        sql TEXT, rows_affected INTEGER, status TEXT)""",
    """CREATE TABLE IF NOT EXISTS loop_state (
        table_name TEXT PRIMARY KEY,
        round INTEGER, status TEXT, last_ts TEXT)""",
]


def _conn():
    os.makedirs(os.path.dirname(AUDIT_DB), exist_ok=True)
    con = sqlite3.connect(AUDIT_DB)
    for ddl in _SCHEMA:
        con.execute(ddl)
    return con


def log(table_name: str, action: str, detail: str = ""):
    """写审计日志(扫描/修复/验证/归档/升级都走这里)"""
    con = _conn()
    con.execute(
        "INSERT INTO audit_log (ts, table_name, action, detail) VALUES (?,?,?,?)",
        (datetime.now().isoformat(), table_name, action, str(detail)[:500]),
    )
    con.commit()
    con.close()


def record_fix(table_name: str, anomaly_key: str, sql: str, rows_affected: int, status: str):
    """记录一次落库修复"""
    con = _conn()
    con.execute(
        "INSERT INTO fix_records (ts, table_name, anomaly_key, sql, rows_affected, status) VALUES (?,?,?,?,?,?)",
        (datetime.now().isoformat(), table_name, anomaly_key, sql[:2000], rows_affected, status),
    )
    con.commit()
    con.close()


def set_loop_state(table_name: str, round_num: int, status: str):
    con = _conn()
    con.execute(
        "INSERT INTO loop_state (table_name, round, status, last_ts) VALUES (?,?,?,?) "
        "ON CONFLICT(table_name) DO UPDATE SET round=excluded.round, status=excluded.status, last_ts=excluded.last_ts",
        (table_name, round_num, status, datetime.now().isoformat()),
    )
    con.commit()
    con.close()


def list_escalations() -> list:
    """未处理的升级清单(最近 50 条 escalate 审计)"""
    con = _conn()
    rows = con.execute(
        "SELECT table_name, detail, ts FROM audit_log "
        "WHERE action='escalate' ORDER BY ts DESC LIMIT 50").fetchall()
    con.close()
    return [{"table": r[0], "detail": r[1], "ts": r[2]} for r in rows]


def get_report(table_name: str) -> dict:
    """质检报告:某表的修复记录 + 审计"""
    con = _conn()
    fixes = con.execute(
        "SELECT anomaly_key, rows_affected, status, ts FROM fix_records WHERE table_name=? ORDER BY ts",
        (table_name,)).fetchall()
    audits = con.execute(
        "SELECT action, detail, ts FROM audit_log WHERE table_name=? ORDER BY ts DESC LIMIT 20",
        (table_name,)).fetchall()
    state = con.execute(
        "SELECT round, status, last_ts FROM loop_state WHERE table_name=?", (table_name,)).fetchone()
    con.close()
    return {
        "table": table_name,
        "fix_records": [{"anomaly_key": f[0], "rows_affected": f[1], "status": f[2], "ts": f[3]} for f in fixes],
        "audit": [{"action": a[0], "detail": a[1], "ts": a[2]} for a in audits],
        "loop_state": {"round": state[0], "status": state[1], "last_ts": state[2]} if state else None,
    }

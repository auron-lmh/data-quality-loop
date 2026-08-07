# -*- coding: utf-8 -*-
"""SQLite 审计层单元测试"""
import pytest

import data_quality_loop.audit as audit


@pytest.fixture
def tmp_audit(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "AUDIT_DB", str(tmp_path / "test_audit.db"))
    return audit


def test_log_and_get_report(tmp_audit):
    tmp_audit.log("orders", "scan", "发现 4 个异常")
    tmp_audit.log("orders", "finalize", "质检完成")
    r = tmp_audit.get_report("orders")
    actions = [a["action"] for a in r["audit"]]
    assert "scan" in actions and "finalize" in actions


def test_record_fix(tmp_audit):
    tmp_audit.record_fix("orders", "orders.amount.empty_rate", "UPDATE orders SET amount=0 WHERE amount IS NULL", 300, "applied")
    r = tmp_audit.get_report("orders")
    assert len(r["fix_records"]) == 1
    assert r["fix_records"][0]["rows_affected"] == 300
    assert r["fix_records"][0]["status"] == "applied"


def test_loop_state_upsert(tmp_audit):
    tmp_audit.set_loop_state("orders", 1, "completed")
    tmp_audit.set_loop_state("orders", 2, "escalated")      # 同表覆盖
    r = tmp_audit.get_report("orders")
    assert r["loop_state"]["round"] == 2
    assert r["loop_state"]["status"] == "escalated"


def test_list_escalations(tmp_audit):
    tmp_audit.log("orders", "escalate", "3 轮未收敛")
    es = tmp_audit.list_escalations()
    assert len(es) == 1
    assert es[0]["table"] == "orders"

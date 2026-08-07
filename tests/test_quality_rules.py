# -*- coding: utf-8 -*-
"""质量规则引擎单元测试:6 类检测在内存 DuckDB 上验证"""
import duckdb
import pytest

from data_quality_loop.quality_rules import (
    run_quality_check, load_rules, scan_all,
)


def _conn():
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE orders (order_id VARCHAR, customer_id VARCHAR, amount DECIMAL, order_date VARCHAR, status VARCHAR)")
    con.execute("""INSERT INTO orders VALUES
        ('O1', 'C1', 100, '2026-01-01', 'paid'),
        ('O1', 'C2', 200, '2026/01/02', 'paid'),      -- 主键重复 + 日期格式错
        ('O2', 'C999', NULL, '2026-01-03', 'bad'),    -- 引用悬空 + 空值 + 枚举错
        ('O3', 'C2', 300, '2026-01-04', 'paid')""")
    con.execute("CREATE TABLE customers (customer_id VARCHAR)")
    con.execute("INSERT INTO customers VALUES ('C1'), ('C2')")
    return con


def test_pk_duplicates():
    con = _conn()
    ans = run_quality_check(con, "orders", [{"type": "pk_duplicates", "columns": ["order_id"]}])
    assert len(ans) == 1
    assert ans[0].key == "orders.order_id.pk_duplicates"
    assert ans[0].evidence_rows == 1              # 多余 1 行


def test_empty_rate():
    con = _conn()
    ans = run_quality_check(con, "orders", [{"type": "empty_rate", "column": "amount", "threshold": 0.0}])
    assert len(ans) == 1
    assert ans[0].key == "orders.amount.empty_rate"
    assert ans[0].evidence_rows == 1


def test_date_format():
    con = _conn()
    ans = run_quality_check(con, "orders", [{"type": "date_format", "column": "order_date", "pattern": r"^\d{4}-\d{2}-\d{2}$"}])
    assert len(ans) == 1
    assert ans[0].key == "orders.order_date.date_format"
    assert ans[0].evidence_rows == 1


def test_reference_integrity():
    con = _conn()
    ans = run_quality_check(con, "orders", [{"type": "reference_integrity", "column": "customer_id", "ref_table": "customers", "ref_column": "customer_id"}])
    assert len(ans) == 1
    assert ans[0].key == "orders.customer_id.reference_integrity"
    assert ans[0].evidence_rows == 1              # C999 悬空


def test_value_enum():
    con = _conn()
    ans = run_quality_check(con, "orders", [{"type": "value_enum", "column": "status", "allowed": ["pending", "paid", "shipped", "completed", "refunded"]}])
    assert len(ans) == 1
    assert ans[0].key == "orders.status.value_enum"
    assert ans[0].evidence_rows == 1              # 'bad' 非法


def test_amount_reconciliation():
    con = _conn()
    con.execute("CREATE TABLE orders_daily_summary (order_date DATE, gmv DECIMAL)")
    con.execute("INSERT INTO orders_daily_summary VALUES ('2026-01-01', 100), ('2026-01-03', 50), ('2026-01-04', 300)")
    ans = run_quality_check(con, "orders_daily_summary", [{
        "type": "amount_reconciliation",
        "detail_table": "orders", "detail_column": "amount", "detail_date_column": "order_date",
        "summary_column": "gmv", "summary_date_column": "order_date", "tolerance": 0.5,
    }])
    assert len(ans) == 1
    assert ans[0].key == "orders_daily_summary.gmv.amount_reconciliation"
    # 2026-01-03: 明细 COALESCE(NULL,0)=0 vs 汇总 50 → 不平


def test_no_anomaly_on_clean():
    con = _conn()
    # 干净表的 value_enum 应无异常
    con.execute("UPDATE orders SET status='paid' WHERE status='bad'")
    ans = run_quality_check(con, "orders", [{"type": "value_enum", "column": "status", "allowed": ["pending", "paid", "shipped", "completed", "refunded"]}])
    assert ans == []


def test_load_rules_and_scan_all(tmp_path):
    import os
    import yaml
    rules = {"orders": [{"type": "empty_rate", "column": "amount", "threshold": 0.0}]}
    p = tmp_path / "rules.yaml"
    p.write_text(yaml.safe_dump({"tables": rules}), encoding="utf-8")
    cfg = load_rules(str(p))
    con = _conn()
    ans = scan_all(con, cfg)
    assert len(ans) == 1

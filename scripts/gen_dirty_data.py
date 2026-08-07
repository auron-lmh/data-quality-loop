# -*- coding: utf-8 -*-
"""
Data_Quality_Loop — 造脏数据(带已知问题)+ 已知问题清单

生成: data/warehouse.duckdb(被质检数仓)+ eval/known_issues.json(评测基准)
固定种子 SEED=42 可复现。埋入 7 个已知问题(6 类规则),供 M2 循环收敛评测。

运行: python scripts/gen_dirty_data.py
"""
import os, sys, json
import numpy as np
import pandas as pd
import duckdb

# 路径
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT, "data", "warehouse.duckdb")
ISSUES_PATH = os.path.join(ROOT, "eval", "known_issues.json")

# ── 埋点参数(全部已知,用于评测比对) ──
SEED = 42
N_CUST = 10_000
N_ORDERS = 50_000
N_DUP_PK = 30           # orders.order_id 主键重复组数(每组 2 行)
N_REF_BAD = 50          # orders.customer_id 引用悬空行数(引用了不存在的客户)
N_EMPTY_AMOUNT = 300    # orders.amount 空值行数
N_DATE_BAD = 100        # orders.order_date 日期格式错误行数('YYYY/MM/DD')
N_EMPTY_PHONE = 300     # customers.phone 空值行数
N_ENUM_BAD = 200        # customers.vip_level 类型不一致行数(数字混入)
N_RECON_DAYS = 5        # 金额勾稽不平的天数(每天 summary 比实际少 100)

LEVELS = ["普通", "银卡", "金卡", "钻石"]
STATUSES = ["pending", "paid", "shipped", "completed", "refunded"]


def _n_rows_idx(rng, n_rows, size):
    """随机选 size 个不重复的行索引"""
    return set(rng.choice(n_rows, size=size, replace=False).astype(int).tolist())


def main():
    rng = np.random.default_rng(SEED)
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    os.makedirs(os.path.dirname(ISSUES_PATH), exist_ok=True)
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    con = duckdb.connect(DB_PATH)

    # ═══ 1. 品类维表(正常) ═══
    cat_df = pd.DataFrame({
        "category_id": [f"CAT{i:03d}" for i in range(1, 11)],
        "category_name": [f"品类{i}" for i in range(1, 11)],
        "tier": ["一线" if i % 2 else "二线" for i in range(1, 11)],
    })
    con.register("df_cat", cat_df)
    con.execute("CREATE TABLE category_map AS SELECT * FROM df_cat")

    # ═══ 2. 客户表(埋 phone 空值 + vip_level 类型不一致) ═══
    cust_ids = [f"C{i:05d}" for i in range(N_CUST)]
    bad_phone = _n_rows_idx(rng, N_CUST, N_EMPTY_PHONE)
    bad_enum = _n_rows_idx(rng, N_CUST, N_ENUM_BAD)

    phone = [f"13{rng.integers(10**9, 10**10):09d}" for _ in range(N_CUST)]
    for i in bad_phone:
        phone[i] = None
    vip = [LEVELS[int(rng.integers(0, 4))] for _ in range(N_CUST)]
    for i in bad_enum:
        vip[i] = str(int(rng.integers(0, 4)))            # 埋数字 '0'~'3'

    days_back = rng.integers(0, 730, size=N_CUST)
    register = (np.datetime64("2025-01-01") - days_back.astype("timedelta64[D]")).astype(str)

    cust_df = pd.DataFrame({
        "customer_id": cust_ids, "phone": phone, "vip_level": vip, "register_date": register,
    })
    con.register("df_cust", cust_df)
    con.execute("CREATE TABLE customers AS "
                "SELECT customer_id, phone, vip_level, CAST(register_date AS DATE) AS register_date FROM df_cust")

    # ═══ 3. 订单表(埋主键重复 / 引用悬空 / 金额空值 / 日期格式错) ═══
    n_unique = N_ORDERS - N_DUP_PK
    oid_unique = [f"O{i:06d}" for i in range(n_unique)]
    dup_idx = rng.choice(n_unique, size=N_DUP_PK, replace=False).astype(int)
    dup_ids = [f"O{int(i):06d}" for i in dup_idx]         # 30 组重复,每组 2 行
    all_oid = oid_unique + dup_ids                        # 总行数 = N_ORDERS

    ref_bad = _n_rows_idx(rng, n_unique, N_REF_BAD)
    empty_amt = _n_rows_idx(rng, n_unique, N_EMPTY_AMOUNT)
    bad_date = _n_rows_idx(rng, n_unique, N_DATE_BAD)

    cust_idx = rng.integers(0, N_CUST, size=n_unique)
    cust_col = [f"C{int(i):05d}" for i in cust_idx]
    for i in ref_bad:
        cust_col[i] = "C99999"                            # 引用不存在的客户

    amount_np = rng.uniform(50, 10000, size=n_unique).round(2)
    amount = [None if i in empty_amt else float(amount_np[i]) for i in range(n_unique)]

    days_back = rng.integers(0, 365, size=n_unique)
    dates = (np.datetime64("2026-01-01") - days_back.astype("timedelta64[D]")).astype(str)
    for i in bad_date:
        dates[i] = dates[i].replace("-", "/")             # 'YYYY/MM/DD'

    pid = [f"P{int(i):05d}" for i in rng.integers(1, 1000, size=n_unique)]
    status = [STATUSES[int(i)] for i in rng.integers(0, 5, size=n_unique)]

    # 重复主键行的其余字段单独生成
    dup_cust = [f"C{int(i):05d}" for i in rng.integers(0, N_CUST, size=N_DUP_PK)]
    dup_amount = rng.uniform(50, 10000, size=N_DUP_PK).round(2).tolist()
    dup_dates = (np.datetime64("2026-01-01") -
                 rng.integers(0, 365, size=N_DUP_PK).astype("timedelta64[D]")).astype(str).tolist()
    dup_pid = [f"P{int(i):05d}" for i in rng.integers(1, 1000, size=N_DUP_PK)]
    dup_status = [STATUSES[int(i)] for i in rng.integers(0, 5, size=N_DUP_PK)]

    ord_df = pd.DataFrame({
        "order_id": all_oid,
        "customer_id": cust_col + dup_cust,
        "amount": amount + dup_amount,
        "order_date": dates.tolist() + dup_dates,
        "product_id": pid + dup_pid,
        "status": status + dup_status,
    })
    con.register("df_ord", ord_df)
    con.execute("CREATE TABLE orders AS "
                "SELECT order_id, customer_id, CAST(amount AS DECIMAL(12,2)) AS amount, "
                "order_date, product_id, status FROM df_ord")

    # ═══ 4. 日汇总表(先正确聚合,再改小 5 天 → 勾稽不平) ═══
    con.execute("""
        CREATE TABLE orders_daily_summary AS
        SELECT TRY_STRPTIME(CAST(order_date AS VARCHAR), '%Y-%m-%d') AS order_date,
               SUM(COALESCE(amount, 0)) AS gmv
        FROM orders GROUP BY 1
    """)
    recon_dates = [r[0] for r in con.execute(
        "SELECT order_date FROM orders_daily_summary WHERE order_date IS NOT NULL "
        "ORDER BY order_date LIMIT ?", [N_RECON_DAYS]).fetchall()]
    for d in recon_dates:
        con.execute("UPDATE orders_daily_summary SET gmv = gmv - 100 WHERE order_date = ?", [d])

    con.close()

    # ═══ 写已知问题清单(评测基准) ═══
    issues = [
        {"key": "orders.order_id.pk_duplicates",         "category": "主键重复",   "expected_evidence": N_DUP_PK},
        {"key": "orders.customer_id.reference_integrity", "category": "引用悬空",   "expected_evidence": N_REF_BAD},
        {"key": "orders.amount.empty_rate",               "category": "空值",       "expected_evidence": N_EMPTY_AMOUNT},
        {"key": "orders.order_date.date_format",          "category": "日期格式",   "expected_evidence": N_DATE_BAD},
        {"key": "orders_daily_summary.gmv.amount_reconciliation", "category": "金额勾稽", "expected_evidence": N_RECON_DAYS},
        {"key": "customers.phone.empty_rate",             "category": "空值",       "expected_evidence": N_EMPTY_PHONE},
        {"key": "customers.vip_level.value_enum",         "category": "类型不一致", "expected_evidence": N_ENUM_BAD},
    ]
    with open(ISSUES_PATH, "w", encoding="utf-8") as f:
        json.dump({"seed": SEED, "known_issues": issues}, f, ensure_ascii=False, indent=2)

    print("✅ 脏数据生成完成")
    print(f"   customers         : {len(cust_df)} 行")
    print(f"   orders            : {len(ord_df)} 行")
    print(f"   category_map      : 10 行")
    print(f"   已知问题          : {len(issues)} 个 → {ISSUES_PATH}")
    print(f"   数仓              : {DB_PATH}")


if __name__ == "__main__":
    main()

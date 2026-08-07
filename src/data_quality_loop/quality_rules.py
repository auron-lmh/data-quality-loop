# -*- coding: utf-8 -*-
"""
质量规则引擎 —— 对 DuckDB 数仓执行规则,输出结构化异常清单

设计(对齐技术方案 §3):
- 规则与配置解耦:引擎只实现 6 类检测,具体表/字段/阈值写在 configs/quality_rules.yaml
- 输出 Anomaly(含 key=表.字段.规则),key 用于 M2 循环收敛比对 + 评测
"""
import os
from dataclasses import dataclass
from typing import List, Dict, Any, Optional

import yaml


@dataclass
class Anomaly:
    """一条质量异常"""
    table: str
    column: str
    rule: str
    detail: str
    evidence_rows: int
    sample: str = ""

    @property
    def key(self) -> str:
        return f"{self.table}.{self.column}.{self.rule}"

    def to_dict(self) -> dict:
        return {
            "table": self.table, "column": self.column, "rule": self.rule,
            "detail": self.detail, "evidence_rows": self.evidence_rows,
            "sample": self.sample, "key": self.key,
        }

    def __repr__(self) -> str:
        return f"[{self.key}] {self.detail}"


# ═══════════════════════════════════════════════════════════════
# 六类检测函数(每类返回 Anomaly 或 None)
# ═══════════════════════════════════════════════════════════════

def _empty_rate(con, table, column, threshold=0.0) -> Optional[Anomaly]:
    """空值率:超过阈值即异常"""
    total, filled = con.execute(
        f'SELECT COUNT(*), COUNT("{column}") FROM "{table}"').fetchone()
    empty = total - filled
    if total and empty / total > threshold:
        return Anomaly(table, column, "empty_rate",
                       f"{column} 空值 {empty} 行({empty / total:.2%})", empty)
    return None


def _pk_duplicates(con, table, columns) -> Optional[Anomaly]:
    """主键唯一性:分组计数 >1 即重复"""
    cols = ", ".join(f'"{c}"' for c in columns)
    rows = con.execute(
        f'SELECT {cols}, COUNT(*) AS c FROM "{table}" GROUP BY {cols} HAVING COUNT(*) > 1').fetchall()
    if rows:
        dup_rows = sum(r[-1] for r in rows) - len(rows)      # 多余行数
        sample = "; ".join(str(r[:len(columns)]) for r in rows[:5])
        return Anomaly(table, ",".join(columns), "pk_duplicates",
                       f"主键 {columns} 重复 {len(rows)} 组,多余 {dup_rows} 行",
                       dup_rows, sample)
    return None


def _date_format(con, table, column, pattern) -> Optional[Anomaly]:
    """日期格式:用正则校验非 NULL 值"""
    bad = con.execute(
        f'SELECT COUNT(*) FROM "{table}" WHERE "{column}" IS NOT NULL '
        f'AND NOT regexp_matches(CAST("{column}" AS VARCHAR), ?)', [pattern]).fetchone()[0]
    if bad:
        return Anomaly(table, column, "date_format",
                       f"{column} 有 {bad} 行不符合日期格式", bad)
    return None


def _reference_integrity(con, table, column, ref_table, ref_column) -> Optional[Anomaly]:
    """引用完整性:左连接找悬空外键"""
    bad = con.execute(
        f'SELECT COUNT(*) FROM "{table}" t '
        f'LEFT JOIN "{ref_table}" r ON t."{column}" = r."{ref_column}" '
        f'WHERE t."{column}" IS NOT NULL AND r."{ref_column}" IS NULL').fetchone()[0]
    if bad:
        return Anomaly(table, column, "reference_integrity",
                       f"{column} 有 {bad} 行引用不存在的 {ref_table}.{ref_column}", bad)
    return None


def _value_enum(con, table, column, allowed) -> Optional[Anomaly]:
    """枚举校验:值不在白名单内即视为类型/取值不一致"""
    marks = ", ".join("?" * len(allowed))
    bad = con.execute(
        f'SELECT COUNT(*) FROM "{table}" WHERE "{column}" IS NOT NULL '
        f'AND "{column}" NOT IN ({marks})', allowed).fetchone()[0]
    if bad:
        return Anomaly(table, column, "value_enum",
                       f"{column} 有 {bad} 行值不在枚举 {allowed} 内", bad)
    return None


def _amount_reconciliation(con, table, detail_table, detail_column, detail_date_column,
                           summary_column, summary_date_column, tolerance=0.5) -> Optional[Anomaly]:
    """金额勾稽:明细按日求和 vs 汇总,差异超容差即异常"""
    sql = f'''
    WITH detail AS (
        SELECT TRY_STRPTIME(CAST("{detail_date_column}" AS VARCHAR), '%Y-%m-%d') AS d,
               SUM(COALESCE("{detail_column}", 0)) AS v
        FROM "{detail_table}" GROUP BY 1
    )
    SELECT d.d, d.v, s."{summary_column}", d.v - s."{summary_column}" AS diff
    FROM detail d
    JOIN "{table}" s ON s."{summary_date_column}" = d.d
    WHERE ABS(d.v - s."{summary_column}") > {tolerance}
    '''
    rows = con.execute(sql).fetchall()
    if rows:
        sample = "; ".join(f"{r[0]}: 明细{r[1]} vs 汇总{r[2]}(差{r[3]})" for r in rows[:5])
        return Anomaly(table, summary_column, "amount_reconciliation",
                       f"{table}.{summary_column} 与 {detail_table} 按日求和有 {len(rows)} 天不平",
                       len(rows), sample)
    return None


RULES = {
    "empty_rate": _empty_rate,
    "pk_duplicates": _pk_duplicates,
    "date_format": _date_format,
    "reference_integrity": _reference_integrity,
    "value_enum": _value_enum,
    "amount_reconciliation": _amount_reconciliation,
}


# ═══════════════════════════════════════════════════════════════
# 对外接口
# ═══════════════════════════════════════════════════════════════

def load_rules(path: str) -> Dict[str, List[Dict[str, Any]]]:
    """加载 rules 配置: {table: [rule,...]}"""
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)["tables"]


def run_quality_check(con, table: str, rules: List[Dict[str, Any]]) -> List[Anomaly]:
    """对单表跑全部规则,返回异常清单"""
    anomalies = []
    for rule in rules:
        fn = RULES.get(rule.get("type"))
        if fn is None:
            continue
        params = {k: v for k, v in rule.items() if k != "type"}
        a = fn(con, table, **params)
        if a is not None:
            anomalies.append(a)
    return anomalies


def scan_all(con, rules_config: Dict[str, List[Dict[str, Any]]]) -> List[Anomaly]:
    """扫描全部配置表,汇总异常"""
    anomalies = []
    for table, rules in rules_config.items():
        anomalies.extend(run_quality_check(con, table, rules))
    return anomalies

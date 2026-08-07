# -*- coding: utf-8 -*-
"""
M1 验收:扫描数仓,验证能发现全部已知问题(与 eval/known_issues.json 比对)

运行: python scripts/scan_check.py
"""
import os, sys, json
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import duckdb
from data_quality_loop.quality_rules import load_rules, scan_all

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "warehouse.duckdb")
RULES = os.path.join(ROOT, "configs", "quality_rules.yaml")
KNOWN = os.path.join(ROOT, "eval", "known_issues.json")


def main():
    con = duckdb.connect(DB, read_only=True)
    cfg = load_rules(RULES)
    ans = scan_all(con, cfg)
    con.close()

    found = {a.key: a for a in ans}
    with open(KNOWN, encoding="utf-8") as f:
        known = json.load(f)["known_issues"]

    print("=== 扫描结果 ===")
    for a in ans:
        print(f"  [{a.key}] {a.detail}")
    print(f"\n共发现 {len(ans)} 个异常")

    print("\n=== 与已知问题比对 ===")
    ok = True
    for k in known:
        a = found.get(k["key"])
        if a is None:
            print(f"  ❌ 未发现 {k['key']}(预期 {k['expected_evidence']})")
            ok = False
        elif a.evidence_rows != k["expected_evidence"]:
            print(f"  ⚠️  {k['key']}: 发现 {a.evidence_rows} 行,预期 {k['expected_evidence']}")
        else:
            print(f"  ✅ {k['key']}: {a.evidence_rows} 行 ✓")

    known_keys = {k["key"] for k in known}
    extra = [a for a in ans if a.key not in known_keys]
    for a in extra:
        print(f"  ℹ️  额外异常(未在已知清单): {a.key}")

    print("\n✅ M1 验收通过" if ok else "\n❌ M1 验收未通过")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

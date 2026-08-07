# -*- coding: utf-8 -*-
"""
收敛评测 —— 证明 Loop 真的把已知问题修完了

指标:
  1. 修复率   = 已消除的已知问题 / 全部已知问题
  2. 残留     = 循环结束后仍存在的问题数
  3. 新问题率 = 修复引入的、不在已知清单里的新异常数
  4. 轮次分布 = 每表收敛轮数

运行:
  python -m eval.run_eval                 # 全部表
  python -m eval.run_eval --tables orders # 单表(调试用)
"""
import os, sys, json, argparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, ROOT)

from data_quality_loop.data_quality_loop import (
    DataQualityLoop, con, _rules, _run_qc, _tables,
)
from data_quality_loop.quality_rules import scan_all

KNOWN_PATH = os.path.join(ROOT, "eval", "known_issues.json")


def load_known():
    with open(KNOWN_PATH, encoding="utf-8") as f:
        return json.load(f)["known_issues"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tables", type=str, default=",".join(_tables), help="逗号分隔的表名")
    ap.add_argument("--max-rounds", type=int, default=3)
    args = ap.parse_args()
    tables = [t.strip() for t in args.tables.split(",") if t.strip()]

    known = load_known()
    # 只统计本次处理表的已知问题(单表调试时,其他表的问题本就不处理)
    known = [k for k in known if any(k["key"].startswith(t + ".") for t in tables)]
    known_map = {k["key"]: k for k in known}

    # 跑前扫描(只看本次处理的表)
    before = {a.key: a for a in scan_all(con, _rules) if any(a.key.startswith(t + ".") for t in tables)}
    print(f"=== 跑前异常: {len(before)} 个(涉及本表已知 {len(known)} 个)===")

    loop = DataQualityLoop()
    results = []
    for t in tables:
        r = loop.process_one(t, max_rounds=args.max_rounds)
        results.append(r)

    # 跑后扫描(只看本次处理的表)
    after = {a.key: a for a in scan_all(con, _rules) if any(a.key.startswith(t + ".") for t in tables)}
    print(f"\n=== 跑后异常: {len(after)} 个 ===")

    # 指标统计
    fixed = 0
    residual = []
    for k in known:
        key = k["key"]
        if key not in after:
            fixed += 1
        else:
            residual.append(key)
    new_issues = [a.key for a in after.values() if a.key not in known_map]

    rate = fixed / len(known) * 100 if known else 0
    print("\n" + "=" * 60)
    print("  📊 收敛评测报告")
    print("=" * 60)
    for r in results:
        icon = "✅" if r["status"] == "completed" else "🆘"
        print(f"  {icon} {r['table']}: {r['status']}({r['rounds']}轮)")
    print(f"\n  修复率      : {rate:.1f}% ({fixed}/{len(known)})")
    print(f"  残留问题    : {len(residual)} 个 {residual if residual else ''}")
    print(f"  新引入问题  : {len(new_issues)} 个 {new_issues if new_issues else ''}")
    rounds = [r["rounds"] for r in results]
    print(f"  平均收敛轮数: {sum(rounds) / len(rounds):.1f}" if rounds else "")
    print("=" * 60)

    ok = len(residual) == 0 and len(new_issues) == 0
    print("\n✅ 收敛评测通过" if ok else "\n❌ 收敛评测未通过(残留或新问题)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

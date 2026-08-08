# -*- coding: utf-8 -*-
"""
数据质量巡检循环系统 (Loop Engineering)

业务场景: 监控 DuckDB 数仓 → 发现数据质量问题 → Fixer 生成修复方案 →
           Verifier 副本 dry-run 验证 → Orchestrator 验证通过后落库 → 收敛或升级人工。

Loop Engineering 核心设计:
  ★ Python for 循环控制重试(代码决定,不是 prompt 决定)
  ★ checkpointer + thread_id 上下文持久化(每轮消息完全一致,LLM 从历史知道该做什么)
  ★ Fixer(Maker-生成) / Verifier(Checker-只读副本验证) / Orch(写-落库) 三层隔离
  ★ 副本 dry-run 先行:修复先在 sandbox 验证,通过才落库(工程安全)
  ★ SQLite 审计:每次落库/升级写审计,跨会话持久化

运行:
  python -m data_quality_loop.data_quality_loop --interval 30 --max-rounds 3
"""
import os, sys, time, json, io, threading
import logging
from datetime import datetime
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

import duckdb
from langchain.tools import tool
from deepagents import create_deep_agent
from deepagents.middleware.subagents import SubAgent
from deepagents.backends import FilesystemBackend
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from data_quality_loop.my_llm import deepseek_llm, deepseek_llm_flash
from data_quality_loop import audit
from data_quality_loop.quality_rules import load_rules as _load_rules, run_quality_check as _run_qc

# ═══════════════════════════════════════════════════════════════
# 路径与连接
# ═══════════════════════════════════════════════════════════════
DB_PATH = os.path.join(ROOT, "data", "warehouse.duckdb")
RULES_PATH = os.path.join(ROOT, "configs", "quality_rules.yaml")
SKILLS = os.path.join(ROOT, "skills")

con = duckdb.connect(DB_PATH)                       # 主库连接(读写,供落库)
_rules = _load_rules(RULES_PATH)
_tables = list(_rules.keys())

# deepagents 在线程池中并发执行工具,而 duckdb 连接非线程安全 → 必须加锁
_DB_LOCK = threading.Lock()

# ═══════════════════════════════════════════════════════════════
# 副本(sandbox)—— 修复验证沙盒,不落主库
# ═══════════════════════════════════════════════════════════════
SANDBOX = None


def _init_sandbox():
    """每轮基于当前主库重建内存副本(所有配置表)。

    注意:不能用 ATTACH 同一文件(主库 con 已打开会文件句柄冲突),
    改为从主库 SELECT 到 pandas 再写入内存副本。
    """
    global SANDBOX
    SANDBOX = duckdb.connect(":memory:")
    with _DB_LOCK:
        for t in _tables:
            df = con.execute(f'SELECT * FROM "{t}"').df()
            SANDBOX.register(f"_df_{t}", df)
            SANDBOX.execute(f'CREATE TABLE "{t}" AS SELECT * FROM _df_{t}')


# ═══════════════════════════════════════════════════════════════
# 工具定义 —— 按权限严格分层
#   只读(Maker+Checker): list_quality_tables / run_quality_check / query_data
#   副本验证(Checker):   apply_fix_to_sandbox
#   写入(Orchestrator):  apply_fix / finalize_quality / escalate_quality
# ═══════════════════════════════════════════════════════════════

@tool
def list_quality_tables() -> str:
    """列出配置了质量规则的表。"""
    return json.dumps(_tables, ensure_ascii=False)


@tool
def run_quality_check(table: str, source: str = "main") -> str:
    """对表跑质量检查,返回异常清单 JSON。
    source=main 查主库;source=sandbox 查副本(Verifier 验证修复效果时用)。"""
    if table not in _rules:
        return f"表 {table} 未配置质量规则。可用表: {_tables}"
    conn = SANDBOX if source == "sandbox" else con
    with _DB_LOCK:
        anoms = _run_qc(conn, table, _rules[table])
    return json.dumps([a.to_dict() for a in anoms], ensure_ascii=False)


@tool
def query_data(table: str, where: str = "", limit: int = 20) -> str:
    """查询表数据(只读,最多 limit 行)。where 为可选的 SQL 过滤条件。"""
    sql = f'SELECT * FROM "{table}"'
    if where:
        sql += f" WHERE {where}"
    sql += f" LIMIT {limit}"
    try:
        with _DB_LOCK:
            return con.execute(sql).df().to_string()
    except Exception as e:
        return f"查询失败: {e}"


@tool
def apply_fix_to_sandbox(sql: str) -> str:
    """在副本上执行修复 SQL(不落主库),返回影响行数。仅 Verifier 调用。"""
    if SANDBOX is None:
        return "错误: 副本未初始化"
    try:
        with _DB_LOCK:
            n = len(SANDBOX.execute(sql).fetchall())
        return f"✅ 副本执行成功,影响 {n} 行"
    except Exception as e:
        return f"❌ 副本执行失败: {e}"


@tool
def apply_fix(table: str, anomaly_key: str, sql: str) -> str:
    """修复验证通过后,在主库落库执行修复 SQL。仅 Orchestrator 调用。"""
    try:
        with _DB_LOCK:
            n = len(con.execute(sql).fetchall())
        audit.record_fix(table, anomaly_key, sql, n, "applied")
        audit.log(table, "apply_fix", f"{anomaly_key} 落库影响 {n} 行")
        return f"✅ 落库成功,{anomaly_key} 影响 {n} 行"
    except Exception as e:
        audit.log(table, "apply_fix_error", str(e))
        return f"❌ 落库失败: {e}"


@tool
def finalize_quality(table: str, comment: str) -> str:
    """质量修复完成,归档。仅 Orchestrator 调用。"""
    audit.log(table, "finalize", comment)
    return f"✅ 表 {table} 质检完成已归档: {comment}"


@tool
def escalate_quality(table: str, reason: str) -> str:
    """升级给人工处理。仅 Orchestrator 调用。"""
    audit.log(table, "escalate", reason)
    return f"🆘 表 {table} 已升级人工: {reason}"


# ═══════════════════════════════════════════════════════════════
# 子代理定义 —— Fixer(Maker-生成) + Verifier(Checker-副本验证)
# ═══════════════════════════════════════════════════════════════

fixer = SubAgent(
    name="fixer",
    description="数据修复员:读异常清单 → 分析根因 → 生成修复方案(SQL)。只生成,不执行。",
    system_prompt="""你是数据修复员(Fixer)。用 run_quality_check(table) 查异常清单,用 query_data 看样本数据。
你只生成修复方案,不执行任何写入操作。

═══════════════════════════════════════════════════
  ⚠️ 修改范围规则(必须严格遵守)
═══════════════════════════════════════════════════
  ★ 情况A:如果 task 描述中列出了具体的问题清单(如「1. xxx 2. xxx ...」)
    → 仅修正清单中列出的问题,逐条对照、逐条修复。
    → 问题清单未提到的内容,一律保持原样,不要重新分析整张表。

  ★ 情况B:如果 task 描述中没有列出具体问题(首轮)
    → 用 run_quality_check 扫描异常,对照 SKILL.md 标准修法逐项生成修复方案。

═══════════════════════════════════════════════════
  修复方案输出格式(JSON,缺一不可)
═══════════════════════════════════════════════════
{
  "fixes": [
    {
      "anomaly_key": "orders.amount.empty_rate",
      "fix_type": "fill_default | dedup | convert | reconcile | correct_format | delete_dangling | fix_enum",
      "sql": "UPDATE orders SET amount = 0 WHERE amount IS NULL RETURNING 1",
      "explain": "补全空值金额为 0",
      "rows_expected": 300
    }
  ]
}

  - 每个异常项对应一条修复;无法自动修复的,在 explain 里说明理由并跳过
  - SQL 必须以 RETURNING 1 结尾(便于统计影响行数)
  - 必须带精确 WHERE,禁止无 WHERE 全表更新

🚫 安全红线:
  - 不引入新问题:修复 SQL 执行后,全表必须通过全部质量规则
  - 影响可控:预计影响行数与实际不符时,优先选择更保守的修复
  - 不要自己验证方案(那是 Verifier 的职责)

输出方案后立即结束。""",
    tools=[list_quality_tables, run_quality_check, query_data],
    model=deepseek_llm,
)

verifier = SubAgent(
    name="verifier",
    description="数据核验员:副本执行修复 → 重跑质检 → 通过/不通过。只读。",
    system_prompt="""你是数据核验员(Verifier)。只有只读和副本验证权限,不能写主库。
编排者会把 Fixer 的修复方案(JSON)写在 task 描述里发给你。

═══════════════════════════════════════════════════
  核验流程
═══════════════════════════════════════════════════
  1. 对方案中每条修复,用 apply_fix_to_sandbox 在副本上执行(sql 已在方案里)
  2. 用 run_quality_check(source="sandbox") 重跑该表全部质量规则
  3. 判断:
     - 目标异常是否全部消失?
     - 是否引入新异常?(修一个坏一个 = 不合格)

═══════════════════════════════════════════════════
  输出格式(必须严格遵守)
═══════════════════════════════════════════════════
  全部达标(目标异常消失 + 无新异常),仅输出:
    审查通过。

  发现问题,输出:
    审查不通过。发现以下问题:
    1. 【异常-key】问题描述 → 修正建议:具体 SQL 或数值
    2. ...

  - 每条修正建议必须具体可操作(给出正确 SQL 或数值)
  - 如果某条修复 SQL 在副本执行报错,也要作为问题列出,附修正建议

⚠️ 约束:
  - 给出审查结论后立即结束,不要写"修改后再来"等循环指令(循环由外部程序控制)
  - 不要修改 Fixer 的方案本身,只判断""",
    tools=[run_quality_check, apply_fix_to_sandbox, query_data, list_quality_tables],
    model=deepseek_llm_flash,
)


# ═══════════════════════════════════════════════════════════════
# 编排者 Agent(编排者不知道"轮次"概念,轮次由外部 Python 控制)
# ═══════════════════════════════════════════════════════════════

def _build_orchestrator():
    return create_deep_agent(
        model=deepseek_llm,
        tools=[apply_fix, finalize_quality, escalate_quality],
        subagents=[fixer, verifier],
        skills=[SKILLS],
        backend=FilesystemBackend(root_dir=ROOT, virtual_mode=True),
        store=InMemoryStore(),
        checkpointer=InMemorySaver(),
        system_prompt="""你是数据质检编排员。你只负责委派子代理和执行落库,不亲自分析数据或验证方案。

═══════════════════════════════════════════════════
  ⚠️ 核心规则:确保 Verifier 的反馈被传递给 Fixer
═══════════════════════════════════════════════════
  首轮:委派 Fixer 全面分析表 X 的异常并生成修复方案。
  续轮:从上一轮 Verifier 输出中提取问题清单,原样复制到 Fixer 的 task 描述:
    "请修复表 {table},仅修正以下问题,其他保持原样:
    1. [从Verifier输出逐条复制的问题和修正建议]
    ..."

  委派 Verifier 时,必须把 Fixer 的完整修复方案(JSON)原样写入 task 描述,
  否则 Verifier 100% 会说自己"没收到方案"。
  ⚠️ 自检:task 描述短于 50 字符说明你没附方案,立即修正。

═══════════════════════════════════════════════════
  每轮执行流程(恰好一轮 Fixer→Verifier,然后立即结束)
═══════════════════════════════════════════════════
  1. 委派 fixer 生成修复方案(首轮全面 / 续轮带问题清单)
  2. 委派 verifier 副本验证 Fixer 的方案
  3. 根据 Verifier 结论:
     - "审查通过" → 对每条修复调用 apply_fix 落库 → 调用 finalize_quality 归档
       → 输出"审查通过",立即结束
     - "审查不通过" → 输出"审查不通过。发现以下问题:" + 全部问题,立即结束
       (外部程序会把这些交给下一轮 Fixer)

  工具权限:
  - 委派子代理(fixer / verifier)
  - apply_fix:★ 仅在 Verifier 审查通过后调用
  - finalize_quality:通过后归档
  - escalate_quality:遇到无法自动修复的问题时升级

  硬性约束(违反将导致系统失控):
  - 禁止自行循环!审查不通过时绝对不要再次委派 Fixer
  - 每次调用最多委派 Fixer 一次、Verifier 一次
  - 未通过 Verifier 审查禁止调用 apply_fix""",
    )


# ═══════════════════════════════════════════════════════════════
# 统计 + 核心循环类
# ═══════════════════════════════════════════════════════════════

@dataclass
class QualityStats:
    scanned: int = 0
    completed: int = 0
    escalated: int = 0
    records: list = field(default_factory=list)


class DataQualityLoop:
    """数据质量巡检循环系统

    Loop Engineering 设计要素:
    - Objective:   数仓异常全部修复、无新问题
    - Trigger:     while True 定时 / FastAPI 触发
    - Discover:    scan() 扫描配置表 + quality_rules
    - Workspace:   副本 sandbox(dry-run)+ DuckDB 主库只读
    - Context:     checkpointer + thread_id + SKILL.md
    - Delegate:    Fixer(生成) + Verifier(副本验证) + Orch(落库)
    - Verify:      Verifier 副本重跑质量规则
    - State:       SQLite 审计
    - Budget:      max_rounds(默认3)
    - Escalate:    循环耗尽 → Python 直接 escalate
    - Exit:        Verifier 通过 → 落库 → 归档
    """

    def __init__(self):
        self.orchestrator = _build_orchestrator()
        self.stats = QualityStats()

    # ── 发现 ──────────────────────────────────────────────

    def scan(self) -> list:
        return list(_tables)

    # ── 单轮执行(每轮消息完全一致,上下文由 checkpointer 携带) ──

    def _run_round(self, table: str, thread_id: str) -> str:
        """执行单轮 Fixer→Verifier,返回编排者本轮输出文本"""
        _init_sandbox()                      # 每轮基于当前主库重建副本
        message = f"请处理表 {table} 的数据质量问题。"
        config = {"configurable": {"thread_id": thread_id}}

        result = self.orchestrator.invoke(
            {"messages": [{"role": "user", "content": message}]},
            config=config,
        )
        # 提取编排者(主 Agent)最后的文本输出
        msgs = result.get("messages", [])
        text = ""
        for m in reversed(msgs):
            if getattr(m, "type", "") == "ai":
                c = getattr(m, "content", "") or ""
                if isinstance(c, list):
                    c = "".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in c)
                text = str(c)
                break
        return text

    # ── 结果判断 ──────────────────────────────────────────

    def _check_result(self, orchestrator_text: str, table: str) -> str | None:
        """完成判断: 主库重跑质检为权威依据（不信任编排者文本自报）。

        背景(修复): 曾出现编排者文本含 "finalize_quality" 即判完成，但实际
        apply_fix 只落库部分修复（4 项异常只落 1 项），导致残留异常被误判收敛。
        → 改为先重跑主库质检: 无残留异常才是真完成。
        编排者文本仅用于: ①质检异常时兜底 ②升级判断（escalate）。
        """
        # 权威: 主库重跑质检无残留异常 → 真完成
        try:
            remaining = _run_qc(con, table, _rules[table])
            if not remaining:
                return "completed"
            logger.warning(
                "主库质检仍有 %d 项残留(%s) → 不判完成（编排者文本不可信）",
                len(remaining),
                table,
            )
        except Exception as e:
            # 质检异常(如表结构变化)时回退编排者文本判断
            logger.warning("主库质检失败(%s), 回退文本判断: %s", table, e)
            if "审查通过" in orchestrator_text or "finalize_quality" in orchestrator_text:
                return "completed"

        # 升级判断（编排者主动升级人工）
        if "escalate_quality" in orchestrator_text or "已升级人工" in orchestrator_text:
            return "escalated"
        return None

    # ── 处理单张表 ────────────────────────────────────────

    def process_one(self, table: str, max_rounds: int = 3) -> dict:
        """Python for 循环控制重试。循环耗尽 → 代码直接升级(不依赖编排者)"""
        thread_id = f"quality-{table}"
        print(f"\n  {'=' * 60}")
        print(f"  📊 处理表: {table}")
        print(f"  {'=' * 60}")

        for round_num in range(1, max_rounds + 1):
            print(f"\n  ╔{'═' * 50}╗")
            print(f"  ║  🔄 Python 循环控制: 第 {round_num}/{max_rounds} 轮")
            print(f"  ╚{'═' * 50}╝")

            text = self._run_round(table, thread_id)
            print(f"\n  [编排者本轮输出]\n{text[:2000]}")

            result = self._check_result(text, table)
            if result == "completed":
                print(f"\n  🎉 [Python 判断] 审查通过 → 表 {table} 质检完成!")
                self.stats.completed += 1
                audit.set_loop_state(table, round_num, "completed")
                return {"table": table, "status": "completed", "rounds": round_num}
            if result == "escalated":
                print(f"\n  🆘 [Python 判断] 编排者主动升级 → 需人工介入")
                self.stats.escalated += 1
                audit.set_loop_state(table, round_num, "escalated")
                return {"table": table, "status": "escalated", "rounds": round_num}

            if round_num < max_rounds:
                print(f"  🔄 [Python 判断] 审查未通过 → 准备第 {round_num + 1} 轮"
                      f"(上下文已持久化,编排者将从历史获取反馈)")

        # 循环耗尽 → 代码直接升级
        print(f"\n  🆘 [Python 判断] {max_rounds} 轮仍未收敛 → 代码直接升级人工")
        escalate_quality.invoke({
            "table": table,
            "reason": f"经过 {max_rounds} 轮 Fixer-Verifier 循环后仍存在质量问题,需人工介入。",
        })
        self.stats.escalated += 1
        audit.set_loop_state(table, max_rounds, "escalated")
        return {"table": table, "status": "escalated", "rounds": max_rounds}

    # ── 单次循环 ──────────────────────────────────────────

    def run_one_cycle(self):
        print(f"\n{'#' * 60}")
        print(f"  🔁 扫描循环 — {datetime.now().strftime('%H:%M:%S')}")
        print(f"{'#' * 60}")

        tables = self.scan()
        self.stats.scanned = len(tables)
        print(f"  🔍 [发现] {len(tables)} 张配置表")

        for t in tables:
            result = self.process_one(t)
            self.stats.records.append(result)
            icon = "✅" if result["status"] == "completed" else "🆘"
            print(f"  └─ {icon} {t}: {result['status']}({result['rounds']}轮)")

        self._report()

    # ── 统计报告 ──────────────────────────────────────────

    def _report(self):
        print(f"\n  📊 [统计] 完成{self.stats.completed} | 升级{self.stats.escalated} | "
              f"扫描{self.stats.scanned}")

    # ── 持续运行 ──────────────────────────────────────────

    def run_loop(self, interval: int = 30):
        print("=" * 60)
        print("  🔄 Loop Engineering: 数据质量巡检循环系统")
        print("  🐍 Python 代码控制循环 | 副本 dry-run | SQLite 审计")
        print("=" * 60)
        print(f"  📂 质检表:     {_tables}")
        print(f"  ⏱️  扫描间隔:   {interval} 秒 | Ctrl+C 停止")
        print(f"  ✏️  Fixer:    数据修复员(生成方案,强模型)")
        print(f"  🔍 Verifier: 数据核验员(副本验证,弱模型)")
        print(f"  🎯 Orchestrator: 编排 + 落库")
        print(f"  💾 持久化:    checkpointer + SQLite 审计")
        print("=" * 60)

        cycle = 0
        try:
            while True:
                cycle += 1
                print(f"\n{'#' * 60}")
                print(f"  🔁 [监控循环 #{cycle}] {datetime.now().strftime('%H:%M:%S')}")
                print(f"{'#' * 60}")
                self.run_one_cycle()
                time.sleep(interval)
        except KeyboardInterrupt:
            print(f"\n\n  🛑 系统已停止。共运行 {cycle} 个监控循环。")


# ═══════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="数据质量巡检循环系统 (Loop Engineering)")
    parser.add_argument("--interval", type=int, default=30, help="扫描间隔(秒)")
    parser.add_argument("--max-rounds", type=int, default=3, help="最大修正轮数")
    parser.add_argument("--once", action="store_true", help="只跑一次扫描就退出")
    args = parser.parse_args()

    loop = DataQualityLoop()
    if args.once:
        loop.run_one_cycle()
    else:
        loop.run_loop(interval=args.interval)

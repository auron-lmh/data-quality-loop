"""收敛判断回归测试 — 主库重跑质检为权威依据，不信任编排者文本自报

背景(修复): 曾出现编排者文本含 "finalize_quality" 即判 completed，
但 apply_fix 实际只落库部分修复（4 项异常只落 1 项）→ 残留异常被误判收敛。
"""

import pytest

from src.data_quality_loop import data_quality_loop as dql_mod
from src.data_quality_loop.data_quality_loop import DataQualityLoop


def _make_loop() -> DataQualityLoop:
    """跳过 __init__（避免构建 DeepAgents 编排者，纯测判断逻辑）"""
    return object.__new__(DataQualityLoop)


class TestCheckResultAuthoritative:
    def test_text_claims_done_but_db_dirty_not_completed(self, monkeypatch):
        """★核心修复: 编排者文本称完成，但主库仍有残留 → 不判完成"""
        loop = _make_loop()
        monkeypatch.setattr(
            dql_mod, "_run_qc", lambda con, table, rules: ["残留异常1", "残留异常2"]
        )
        assert loop._check_result("审查通过 finalize_quality", "orders") is None

    def test_db_clean_completed_even_without_text_marker(self, monkeypatch):
        """主库已干净，即使文本无完成标记 → 判完成"""
        loop = _make_loop()
        monkeypatch.setattr(dql_mod, "_run_qc", lambda con, table, rules: [])
        assert loop._check_result("编排者随便说了些话", "orders") == "completed"

    def test_escalate_takes_priority(self, monkeypatch):
        """编排者文本含升级标记 → 判 escalated（升级优先于残留检查）"""
        loop = _make_loop()
        monkeypatch.setattr(
            dql_mod, "_run_qc", lambda con, table, rules: ["残留"]
        )
        assert (
            loop._check_result("escalate_quality 已升级人工", "orders") == "escalated"
        )

    def test_qc_raises_falls_back_to_text(self, monkeypatch):
        """主库质检异常时回退编排者文本判断（不因质检故障卡死）"""
        loop = _make_loop()

        def _boom(con, table, rules):
            raise RuntimeError("质检失败")

        monkeypatch.setattr(dql_mod, "_run_qc", _boom)
        assert loop._check_result("finalize_quality", "orders") == "completed"
        assert loop._check_result("没有任何标记", "orders") is None

# -*- coding: utf-8 -*-
"""
FastAPI 接入层 —— 承载 Loop 的 Trigger / 异常查询 / 人工介入 / 审计查询

复用同一核心循环(DataQualityLoop),不做第二套逻辑(三端复用一核心)。

运行:
  python -m data_quality_loop.api.app          # http://localhost:8500
  或  uvicorn data_quality_loop.api.app:app --port 8500
"""
import os, sys
from fastapi import FastAPI, Depends, HTTPException
from fastapi.security import HTTPBearer
from pydantic import BaseModel

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

from data_quality_loop.env_utils import API_TOKEN
from data_quality_loop import audit
from data_quality_loop.quality_rules import scan_all
from data_quality_loop.data_quality_loop import con, _rules, _DB_LOCK, DataQualityLoop

app = FastAPI(title="Data Quality Loop API", version="0.1.0",
              description="数据质量巡检循环 —— 扫描异常 / 触发修复 / 审计查询 / 人工介入")

security = HTTPBearer(auto_error=False)

_loop: DataQualityLoop | None = None


def get_loop() -> DataQualityLoop:
    global _loop
    if _loop is None:
        _loop = DataQualityLoop()          # 懒加载:构建编排者较慢
    return _loop


def verify_token(credentials=Depends(security)):
    if not API_TOKEN:                       # 开发模式:留空放行
        return
    if credentials is None or credentials.credentials != API_TOKEN:
        raise HTTPException(401, "token 无效")


class FixRequest(BaseModel):
    # 修复(审查): 边界校验——0/负数空转即 escalate，超大值阻塞数小时
    max_rounds: int = 3

    @classmethod
    def clamp_rounds(cls, v: int) -> int:
        return max(1, min(int(v), 10))


class EscalateResolve(BaseModel):
    action: str = "confirm"                  # confirm / reject
    note: str = ""


# ═══════════════════════════════════════════════════
# 接口
# ═══════════════════════════════════════════════════

@app.get("/health")
def health():
    """健康检查"""
    return {"status": "ok"}


@app.get("/api/tables")
def list_tables(_=Depends(verify_token)):
    """列出配置了质量规则的表"""
    return {"tables": list(_rules.keys())}


@app.get("/api/anomalies")
def anomalies(_=Depends(verify_token)):
    """当前数仓的异常清单(只读扫描,不触发循环)"""
    with _DB_LOCK:
        ans = [a.to_dict() for a in scan_all(con, _rules)]
    return {"count": len(ans), "anomalies": ans}


@app.post("/api/fix/{table}")
def fix_table(table: str, req: FixRequest | None = None, _=Depends(verify_token)):
    """对指定表跑一轮完整质检循环(Fixer→Verifier→落库/升级)。同步,可能耗时几十秒。"""
    if table not in _rules:
        raise HTTPException(404, f"表 {table} 未配置质量规则")
    rounds = FixRequest.clamp_rounds(req.max_rounds) if req else 3
    result = get_loop().process_one(table, max_rounds=rounds)
    return result


@app.post("/api/scan")
def scan_trigger(_=Depends(verify_token)):
    """触发一次全表质检循环(同步跑完所有配置表)"""
    get_loop().run_one_cycle()
    return {"status": "done"}


@app.get("/api/report/{table}")
def report(table: str, _=Depends(verify_token)):
    """质检报告:修复记录 + 审计日志 + 循环状态"""
    return audit.get_report(table)


@app.get("/api/escalations")
def escalations(_=Depends(verify_token)):
    """未处理的升级清单"""
    return audit.list_escalations()


@app.post("/api/escalations/{table}/resolve")
def resolve_escalation(table: str, req: EscalateResolve, _=Depends(verify_token)):
    """人工介入:确认修复已处理 / 驳回"""
    audit.log(table, f"human_{req.action}", req.note or f"人工{req.action}")
    return {"status": "resolved", "table": table, "action": req.action}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("data_quality_loop.api.app:app", host="0.0.0.0", port=8500)

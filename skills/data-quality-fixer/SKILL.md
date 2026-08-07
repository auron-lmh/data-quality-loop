---
name: data-quality-fixer
description: 数据质量修复标准流程。Fixer(数据修复员)负责分析异常并生成修复方案,Verifier(数据核验员)负责在副本上独立复核。编排者负责传递反馈并在验证通过后落库。
---

# 数据质量修复标准流程

> **核心理念**：修复不是一次成功的，而是"Fixer 生成方案 → Verifier 副本验证 → 不通过带问题清单回到 Fixer"的多轮收敛。
> 循环控制由外部 Python 程序负责，编排者只需专注每一轮的 Fixer→Verifier 流程。

---

## 零、Loop 收敛设计（⚠️ 最重要的设计原则）

### 信息闭环流

```
Python 循环控制 → Orchestrator 收到"请处理表 X"
    │
    ├─ 首轮：Fixer 全面分析异常 → 生成修复方案 → Verifier 副本验证 → 问题清单
    │
    └─ 续轮：Orchestrator ★将 Verifier 的问题清单原样传递给 Fixer★
              → Fixer 靶向修正 → Verifier 再验证 → 问题减少或通过
```

### ⚠️ 收敛的核心规则

1. **Orchestrator 必须传递反馈**：续轮时把上一轮 Verifier 的问题清单逐条复制给 Fixer，禁止只说"根据意见修改"
2. **Fixer 只做靶向修复**：收问题清单时仅修清单中的项，未列出的保持原样
3. **Fixer 不得引入新问题**：修复 SQL 自身必须通过全部质量规则（修一个不能坏一个）
4. **Verifier 提供可操作的修正建议**：每条问题附带具体 SQL 或数值
5. **问题清单逐轮缩小**：每轮后剩余问题必须比上一轮少

### 为什么必须这样做

Fixer 每次被委派都是全新状态，看不到上一轮验证结果。Orchestrator 不把问题清单传过去，Fixer 就不知道修什么，会乱修或漏修，Loop 无法收敛。

---

## 一、修复四大原则（Fixer 必须遵守）

1. **靶向修复**：只修异常清单/问题清单中列出的项
2. **不引入新问题**：修复 SQL 执行后，全表必须通过全部质量规则（不只目标异常）
3. **影响可控**：UPDATE/DELETE 必须带精确 WHERE，禁止无 WHERE 全表更新；修复前预估影响行数
4. **SQL 规范**：修复 SQL 必须以 `UPDATE ... RETURNING 1` 或 `DELETE ... RETURNING 1` 结尾，便于统计影响行数

---

## 二、修复方案输出格式（JSON，缺一不可）

```
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
```

- 每个异常项对应一条修复（除非确认无需修复并说明理由）
- 输出方案后立即结束，不要自己验证（那是 Verifier 的职责）

---

## 三、各类问题的标准修法

| 规则 | fix_type | 标准做法 | 示例 |
|------|---------|---------|------|
| empty_rate（空值） | fill_default | 空值填充业务合理默认值（金额=0、文本='未知'）或删除空值行（若不可填充） | `UPDATE orders SET amount=0 WHERE amount IS NULL RETURNING 1` |
| pk_duplicates（主键重复） | dedup | 保留每个重复组中一行（如金额非空/最新），DELETE 其余 | `DELETE FROM orders WHERE order_id IN (...) AND ... RETURNING 1` |
| reference_integrity（引用悬空） | delete_dangling / fix_foreign | 优先删除悬空行；或 UPDATE 到有效外键值 | `DELETE FROM orders WHERE customer_id='C99999' RETURNING 1` |
| date_format（日期格式） | correct_format | UPDATE 统一为标准格式（YYYY-MM-DD） | `UPDATE orders SET order_date=REPLACE(order_date,'/','-') WHERE order_date LIKE '%/%' RETURNING 1` |
| amount_reconciliation（金额勾稽） | reconcile | 修正汇总表，使与明细按日求和一致 | `UPDATE orders_daily_summary SET gmv=gmv+100 WHERE order_date='2026-01-01' RETURNING 1` |
| value_enum（枚举/类型） | fix_enum | 把非法值映射到合法枚举 | `UPDATE customers SET vip_level='普通' WHERE vip_level IN ('0','1','2','3') RETURNING 1` |

---

## 四、Verifier 核验标准

### 核验流程

1. 用 `apply_fix_to_sandbox` 在**副本**上逐条执行 Fixer 的修复 SQL（不落主库）
2. 用 `run_quality_check(source="sandbox")` **重跑该表全部质量规则**
3. 对比修复前后：
   - 目标异常是否消失？
   - 有没有**新引入**的异常？（修一个坏一个 = 不合格）

### 通过/不通过输出

- **通过**（目标异常全部消失 + 无新异常）：仅输出 `审查通过。`
- **不通过**：输出 `审查不通过。发现以下问题：` + 逐条（每条附修正建议，含正确 SQL 或数值）
- ⚠️ 约束：给出结论后立即结束，不要写"修改后再来"等循环指令（循环由外部程序控制）

### 🛑 容忍规则（不算问题）

- 修复 SQL 与建议的写法略有差异（只要逻辑等价、能消异常且无新问题）
- 填充默认值与建议不同（只要业务合理）

---

## 五、退出条件

- **正常退出**：Verifier "审查通过" → Orchestrator `apply_fix` 落库 → `finalize_quality` 归档 → 外部程序停止循环
- **升级退出**：达到最大修正轮数仍未通过 / 修复影响行数过大 → `escalate_quality` 升级人工
- **异常退出**：表不存在或无法读取 → 外部程序记录并跳过

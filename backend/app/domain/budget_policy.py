"""预算的可用额与结转口径（M7）。

这一层是纯 Python，**不 import 任何框架**（理由同 `rules.py` / `purchase.py`：
毫秒级可测，换存储或换入口时一行不用改）。

============================ 这个文件为什么必须存在 ============================
"预留与实付的差额必须显式释放"属于**违反不会报错、只会算错**的那一类：
预留 1000 元、实付 900 元，如果只写了 `SPEND(900)` 而漏掉 `RELEASE(100)`，
那 100 元会**永久挂在 `amount_reserved` 上** —— 账面完全正常、日志一行不报，
可用额度却虚低，而且随着单子越做越多越积越深。

所以这条算术做成**纯函数 + 测试**，而不是散在付款接口里的几行 `if`。

============================ 三条不变量（付款结清时必须成立）============================
1. 可用余额 = `amount_total − amount_reserved − amount_spent`；
2. 实付 **不得超过** 预留（超了说明单据对不上，宁可拒绝也不要记账）；
3. 一笔申请结清后：该申请的 `amount_reserved` **归零**、`amount_total` 全程不变、
   `amount_spent` 只增**实付**金额。

============================ 谁做判断、谁只负责报错 ============================
"够不够"的**判定**在数据库的条件更新里（`WHERE amount_total − (reserved + spent) >= :x`），
这里只负责两件事：① 把"还差多少"算成人话；② 把结转金额算准。
**先算一遍再据此决定放不放行，是并发下必然出错的做法**（见业务规则第 1 条）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

#: 单笔预留 / 结转的最小金额。0 元采购不存在 ——
#: 与 `purchase.check_submittable` 里"金额为 0 不能提交"是同一条口径。
MIN_AMOUNT_FEN = 1


class BudgetError(ValueError):
    """预算口径不成立。边界层把它转成 422，并把 message 原样给用户。"""


def _require_non_negative(value: Any, field: str) -> int:
    """取非负整数。**bool 不算整数**（`True` 在 Python 里能当 1 用，是坑）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise BudgetError(f"{field}必须是整数（单位：分）")
    if value < 0:
        raise BudgetError(f"{field}不能为负数")
    return value


def _require_positive(value: Any, field: str) -> int:
    amount = _require_non_negative(value, field)
    if amount < MIN_AMOUNT_FEN:
        raise BudgetError(f"{field}必须大于 0（单位：分）")
    return amount


def available_fen(
    *, amount_total: int, amount_reserved: int, amount_spent: int
) -> int:
    """可用余额（分）。这是**唯一**允许出现的余额公式。

    三个入参都是"库里当前的数字"；本函数不做任何并发判断 ——
    真正的守卫写在 SQL 的 `WHERE` 里，见模块开头。
    """
    total = _require_non_negative(amount_total, "预算总额")
    reserved = _require_non_negative(amount_reserved, "已预留")
    spent = _require_non_negative(amount_spent, "已花")
    return total - reserved - spent


def check_reservable(
    *,
    amount_total: int,
    amount_reserved: int,
    amount_spent: int,
    amount_fen: int,
) -> None:
    """够不够？不够就抛 `BudgetError`，并把"可用多少 / 需要多少 / 还差多少"说清楚。

    ⚠️ **调用它的时机**：条件更新报了 0 行受影响之后，用它把错误信息写成人话。
    绝不能反过来 —— 先调它、再决定要不要扣（那样并发下必然超额占用）。
    """
    amount = _require_positive(amount_fen, "预留金额")
    available = available_fen(
        amount_total=amount_total,
        amount_reserved=amount_reserved,
        amount_spent=amount_spent,
    )
    if available < amount:
        raise BudgetError(
            f"预算不足：可用 {available} 分，本次需要 {amount} 分"
            f"（还差 {amount - available} 分）"
        )


@dataclass(frozen=True)
class BudgetSettlement:
    """一笔申请结清时要写的两条流水。

    `spend_fen` 记**实付**，`release_fen` 记**差额** —— 两条都必须写。
    """

    spend_fen: int
    release_fen: int

    @property
    def total_fen(self) -> int:
        """这条申请这次一共动用了多少预留（= 当初预留的金额）。"""
        return self.spend_fen + self.release_fen


def settlement(*, reserved_fen: int, actual_fen: int) -> BudgetSettlement:
    """把"预留了多少、实际付了多少"换算成两条流水金额。

    | 情况 | 结果 |
    | --- | --- |
    | 实付 < 预留 | `SPEND(实付)` + `RELEASE(差额)` —— 差额必须显式释放 |
    | 实付 = 预留 | `SPEND(实付)` + `RELEASE(0)`（不写流水） |
    | 实付 > 预留 | **拒绝**：单据对不上（超预留付款要先查清原因，不能记账） |
    | 没有预留 | **拒绝**：说明释放入口被漏掉了，宁可报错也不要把账弄乱 |
    """
    reserved = _require_non_negative(reserved_fen, "预留金额")
    actual = _require_positive(actual_fen, "实付金额")

    if reserved < MIN_AMOUNT_FEN:
        raise BudgetError("这笔申请没有任何预留，不能结转（可能漏了 RESERVE 流水）")
    if actual > reserved:
        raise BudgetError(
            f"实付金额超过预留：预留给 {reserved} 分，实付 {actual} 分"
            f"（多出 {actual - reserved} 分），请先核对单据"
        )
    return BudgetSettlement(spend_fen=actual, release_fen=reserved - actual)


def resolve_budget_year(submitted_at: datetime) -> int:
    """这份申请该占哪一年的预算：**提交时刻所在年度**。

    刻意不用"申请创建时间"：草稿可以跨年躺着（12 月建、次年 1 月提交），
    真正占用额度的是**提交**这个动作。

    用 UTC：与库里 `timestamptz` 的语义一致，也不受服务器本地时区影响 ——
    跨年那一两天差一年，会让"预算到底占在哪一年"变成没人说得清的事。
    """
    return submitted_at.year

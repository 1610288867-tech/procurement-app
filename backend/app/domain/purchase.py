"""采购申请的业务规则（M5）：明细、金额、提交前置条件。

这一层是纯 Python，**不 import 任何框架**（不收 Pydantic 模型，只收 Mapping），
所以它毫秒级可测，也是"先写纯函数再动数据库"这条纪律最直接的落点。

============================ 一条不可让步的约定 ============================
**`amount_total` 由后端按明细重算，前端传的金额一律不信任。**

这条不是"更严谨一点"，而是"账目对不对"的问题：
前端传 1 分、明细加起来 100 万分，信了前端就等于系统里同时存在两个金额。
所以 `PATCH` 接口**根本不接受 `amount_total` 字段**，它是算出来的派生值。

行金额同理：`amount = quantity * unit_price`，也是算出来的，
客户端传什么都不作数（传了也没人读）。

============================ 为什么校验收 Mapping 而不是模型 ============================
和 `domain/rules.py` 同一个理由：这一层因此不依赖 Pydantic；
以后换请求校验库（或从 HTTP 换成命令行入口）时，这些规则一行不用改。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.domain.enums import DEFAULT_URGENCY, URGENCIES

# 一张申请最多几行。定这个上限是为了挡住"一次提交十万行"这种写法，
# 不是业务规则；真正的业务上限该由品类说了算（第一版没有）。
MAX_ITEMS = 100

# 单行数量 / 单价上限。金额一律整数「分」，这里定的是**防溢出**的工程上限：
# 10 亿分 = 1000 万元，任何一条采购明细都不该到这个量级。
MAX_QUANTITY = 1_000_000
MAX_UNIT_PRICE_FEN = 1_000_000_000

#: 草稿上允许客户端直接改的字段。
#: **金额与状态不在其中**：前者是算出来的，后者只能由状态迁移改。
PATCHABLE_FIELDS: frozenset[str] = frozenset(
    {"title", "reason", "urgency", "form_data", "category_id"}
)


class PurchaseError(ValueError):
    """采购申请不合法。边界层把它转成 422，并把 message 原样给用户。"""


@dataclass(frozen=True)
class LineDraft:
    """一行明细（已校验）。金额是算出来的，不是传进来的。"""

    line_no: int
    name: str
    spec: str | None
    quantity: int
    unit_price_fen: int
    amount_fen: int


def _as_int(value: Any, field: str) -> int:
    """取整数。**bool 不算整数**（`True` 在 Python 里能当 1 用，这里是坑）。

    `field` 由调用方给出完整前缀（如 `"1 行：数量"`），这里只接后半句 ——
    两处都拼前缀的话，报错会变成"第 第 1 行：…"这种读不通的句子。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise PurchaseError(f"第 {field}必须是整数")
    return value


def parse_line(raw: Mapping[str, Any], line_no: int) -> LineDraft:
    """校验并归一化一行明细。

    行金额 = 数量 × 单价（单位：分），**不接受客户端传的金额**。
    """
    name = str(raw.get("name") or "").strip()
    if not name:
        raise PurchaseError(f"第 {line_no} 行：名称不能为空")

    spec_raw = raw.get("spec")
    spec = str(spec_raw).strip() if spec_raw is not None else None
    if spec == "":
        spec = None

    quantity = _as_int(raw.get("quantity"), f"{line_no} 行：数量")
    if quantity <= 0:
        raise PurchaseError(f"第 {line_no} 行：数量必须大于 0")
    if quantity > MAX_QUANTITY:
        raise PurchaseError(f"第 {line_no} 行：数量超过上限 {MAX_QUANTITY}")

    # 字段名带 _fen：金额一律整数「分」，写了浮点在这里就被拦下
    unit_price = _as_int(raw.get("unit_price_fen"), f"{line_no} 行：单价")
    if unit_price <= 0:
        raise PurchaseError(f"第 {line_no} 行：单价必须大于 0")
    if unit_price > MAX_UNIT_PRICE_FEN:
        raise PurchaseError(f"第 {line_no} 行：单价超过上限 {MAX_UNIT_PRICE_FEN} 分")

    return LineDraft(
        line_no=line_no,
        name=name,
        spec=spec,
        quantity=quantity,
        unit_price_fen=unit_price,
        amount_fen=quantity * unit_price,
    )


def parse_lines(raw_items: Sequence[Mapping[str, Any]]) -> list[LineDraft]:
    """校验整套明细：逐行校验 + 行号按顺序重排 + 总量上限。

    行号由后端按顺序生成，**不接受客户端传 line_no** ——
    否则客户端可以造出重复或乱序的行号，明细的顺序就没有意义了。
    """
    if not raw_items:
        raise PurchaseError("至少要有 1 行明细")
    if len(raw_items) > MAX_ITEMS:
        raise PurchaseError(f"明细行数超过上限 {MAX_ITEMS}")

    return [parse_line(raw, index + 1) for index, raw in enumerate(raw_items)]


def total_amount_fen(lines: Sequence[LineDraft]) -> int:
    """申请总额 = 各行金额之和。这就是落库的 `amount_total`。"""
    return sum(line.amount_fen for line in lines)


def parse_urgency(value: Any) -> str:
    """紧急程度。空值按默认（NORMAL）处理，非法值直接拒绝。

    不接受"认不出就按 NORMAL"的宽松写法：`URGENT` 拼错成 `URGENTT` 时，
    宽松处理会把一笔加急申请**静默降级**成普通件，而且没人会发现。
    """
    if value is None or value == "":
        return DEFAULT_URGENCY
    if value not in URGENCIES:
        raise PurchaseError(f"urgency 只能是 {' 或 '.join(URGENCIES)}")
    return str(value)


def check_submittable(
    *, status: str, lines: Sequence[LineDraft], amount_total_fen: int
) -> None:
    """提交前的最后一道闸。

    这里查的都是"**提交成功之后就再也没法补救**"的情况：
    申请一旦进入审批，谁都改不动它了（M5 里连撤回都只是终止流程）。
    所以宁可在这里挡住，也不要让一张 0 元或空明细的单子进审批流。
    """
    from app.domain.enums import RequestStatus  # 局部导入，避免循环

    if status != RequestStatus.DRAFT:
        raise PurchaseError("只有草稿可以提交")
    if not lines:
        raise PurchaseError("申请没有任何明细，不能提交")
    if amount_total_fen <= 0:
        raise PurchaseError("申请金额为 0，不能提交")
    # 兜底：金额必须是"明细算出来的那个数"。对不上说明有人绕过重算写了值。
    if amount_total_fen != total_amount_fen(lines):
        raise PurchaseError("申请金额与明细不一致，请重新保存明细")

"""订单与收货的规则（M10 / 8.2）。**纯函数，不 import 任何框架。**

============================ 这个模块守着一条容易被"顺手简化"掉的口径 ============================
**订单金额 = Σ(明细行单价 × 数量)**，由后端按明细重算，**不接受客户端传总额**
（与 M5 的 `amount_total` 同一条纪律）。

而"明细行单价从哪来"按采购方式分两种：

    询价（走过 quotations） → **定标报价单价**（通常 ≤ 申请单价，比价压价）
    直采（没走询价）         → 实际成交单价（默认 = 申请单价；有议价取议价后价）

由此**推出两条**，它们都是"不报错、只会算错"的那一类：

1. **订单金额是"实际要付多少"的唯一基准** —— 三单匹配的"发票 vs 订单"、
   预算结转的"实付 vs 预留"都以它为准，**不是以申请金额为准**。
   所以申请金额与订单金额不一致是**正常的**（差额由 M7 的
   "实付小于预留则显式释放"处理），不是 bug。
2. 于是"取错单价"这件事必须只有一个入口：`build_order_lines`。
   写错一次（例如误用申请单价），订单总额就悄悄变回申请金额，
   而三单匹配会跟着一起错 —— **发票与订单两边都错就互相抵消，谁也不会报错**。

============================ 收货只做整单，**部分收货当场拒绝** ============================
领域设计把部分收货列进了"第一版先砍掉这些"，状态机里也没有
`PARTIALLY_RECEIVED`。所以"收一半"不能"收下但主单不动"：

    允许 → 主单停在 ORDERED，但订单上写着已收货、收货单也建了 ——
           **模型禁止的组合被真实数据造出来**，而且走不到任何终态
           （再收一次会被"已经收过货"挡住）
    拒绝 → 用户改完再提交，主单与订单**一起**进 RECEIVED

于是 `receipt_problems` 负责"当场拒绝并说清差哪几行"。
要恢复部分收货，按实施计划 5.5 节 E 的四步把状态与转移**一起**加回来，
而不是把那一句拒绝删掉。

超收（实收 > 订单数量）同样 422：多收的货没有合同依据，
而"收货数量 ≤ 订单数量"正是三单匹配里那一项的前提
（业务规则第 6 条：`收货数量 ≤ 订单数量`，超收要人工确认）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

#: 收货单状态（第一版只有"已收货"这一档）。
#: 不做 `PARTIALLY_RECEIVED` —— 见模块说明。
RECEIPT_RECEIVED = "RECEIVED"
RECEIPT_STATUSES: tuple[str, ...] = (RECEIPT_RECEIVED,)


class SourcingOrderError(ValueError):
    """订单/收货规则不满足。带 `code` 供边界层转成统一错误体。"""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SourceLine:
    """申请明细行（**输入**，只带规则用得上的字段）。

    刻意不叫 `PurchaseItem`：领域层不认识 ORM 记录，
    协调者从记录里挑字段构造它（与 `QuotationFacts` 同一个套路）。
    """

    item_id: str
    name: str
    spec: str | None
    quantity: int
    #: 申请时的单价（分）—— **只在直采场景**用它当成交单价
    unit_price_fen: int


@dataclass(frozen=True)
class OrderLineDraft:
    """订单明细行（**输出**，可直接落库）。"""

    item_id: str
    name: str
    spec: str | None
    quantity: int
    unit_price_fen: int

    @property
    def amount_fen(self) -> int:
        return self.quantity * self.unit_price_fen


def build_order_lines(
    lines: Sequence[SourceLine], *, deal_unit_price_fen: int | None
) -> list[OrderLineDraft]:
    """把申请明细**复制**成订单明细，并定下单价的来源。

    ============================ 为什么是"复制"而不是"指向" ============================
    订单一旦生成就是**独立凭证**（ER 图第 134-135 行）：申请后续不能改它。
    指向申请明细的话，申请人改一次草稿（理论上提交后不能改，但将来可能有
    "补充式驳回"这类流程）就会把历史订单的金额一起改掉 —— 而订单已经付过款了。

    ============================ 单价怎么定 ============================
        `deal_unit_price_fen` 有值 → **整单统一取它**（询价定标的成交单价）
        `deal_unit_price_fen` 为 None → 逐行取申请单价（直采）

    "整单统一"是刻意的：询价是**按整单**比价的（三家按总额竞争），
    所以成交单价对整张单子是一个数。行级差异（甲行压价、乙行不压）
    属于"行级匹配"那一档复杂度，已在计划里明确砍到 M10 之后。

    ⚠️ 数量必须 > 0：0 数量的行会让订单金额凭空少一块，
    而它在界面上看着像一条正常的明细。
    """
    drafts: list[OrderLineDraft] = []
    for line in lines:
        if line.quantity <= 0:
            raise SourcingOrderError(
                f"明细行「{line.name}」的数量必须大于 0（当前 {line.quantity}）",
                code="ORDER_QUANTITY_INVALID",
            )
        unit_price = (
            deal_unit_price_fen if deal_unit_price_fen is not None else line.unit_price_fen
        )
        if unit_price <= 0:
            raise SourcingOrderError(
                f"明细行「{line.name}」的单价必须大于 0（当前 {unit_price}）",
                code="ORDER_PRICE_INVALID",
            )
        drafts.append(
            OrderLineDraft(
                item_id=line.item_id,
                name=line.name,
                spec=line.spec,
                quantity=line.quantity,
                unit_price_fen=unit_price,
            )
        )
    if not drafts:
        # 空的订单不是"金额为 0"，而是**没有采购内容** —— 必须挡住，
        # 否则后面付款时会走到"三单匹配：订单金额 0 与发票金额比较"这种荒唐分支。
        raise SourcingOrderError("申请没有明细行，无法生成订单", code="ORDER_LINES_EMPTY")
    return drafts


def order_amount_fen(lines: Sequence[OrderLineDraft]) -> int:
    """订单总额（分）= Σ(单价 × 数量)。**唯一**该被用来算订单金额的入口。"""
    return sum(line.amount_fen for line in lines)


def check_receipt(
    *,
    ordered_quantity: int,
    accepted_quantity: int,
    line_name: str = "",
) -> list[str]:
    """一条收货明细能不能收。返回**问题列表**（空 = 可以）。

    与 `check_awardable` 同一个模式：说清"哪里不对"，而不是一个布尔值。

    两条规则：

        实收数量 <= 订单数量     超收要人工确认（业务规则第 6 条）
        实收数量 >= 0            负数收货会让"已收数量"倒退

    **允许实收为 0**（这条货一件都没收）—— 整单收货里"某一行没到货"是常见情形，
    而"一行都没收到"与"这一行不收"是两件事：后者合法，前者由主单状态表达
    （没收齐就不该是 RECEIVED）。
    """
    problems: list[str] = []
    label = f"「{line_name}」" if line_name else ""
    if accepted_quantity < 0:
        problems.append(f"明细{label}的实收数量不能为负数（当前 {accepted_quantity}）")
    # 收货数量 ≤ 订单数量（超收需要人工确认）
    if accepted_quantity > ordered_quantity:
        problems.append(
            f"明细{label}的实收数量 {accepted_quantity} 超过订单数量 {ordered_quantity}，"
            "超收需要人工确认（请联系采购或供应商处理）"
        )
    return problems


def receipt_plan(
    *,
    order_lines: Sequence[tuple[str, int, str]],
    accepted: dict[str, int],
) -> list[tuple[str, int]]:
    """整单收货的计划：返回 `[(订单明细 id, 实收数量), ...]`。

    ============================ 为什么"没填的行"按 0 处理 ============================
    界面上逐行填实收数量，**没填就是没收到**（而不是"跳过这一行"）。
    把它当 0 是唯一说得通的口径：整单收货要求"每一行都有个结论"，
    否则"没收齐"这个事实无处安放 —— 而它正是主单不该变成 `RECEIVED` 的理由。

    三条拒绝：
        订单里没有的行        → 422（有人拿别的单子的行 id 来收货）
        实收数量超过订单数量   → 422（超收）
        实收数量为负          → 422（负数）
    """
    problems: list[str] = []
    plan: list[tuple[str, int]] = []
    known = {line_id for line_id, _, _ in order_lines}

    for line_id in accepted:
        if line_id not in known:
            problems.append(f"明细 {line_id} 不属于这张订单，不能对它收货")
    if problems:
        raise SourcingOrderError("；".join(problems), code="RECEIPT_LINE_UNKNOWN")

    for line_id, ordered_quantity, name in order_lines:
        quantity = int(accepted.get(line_id, 0))
        line_problems = check_receipt(
            ordered_quantity=ordered_quantity,
            accepted_quantity=quantity,
            line_name=name,
        )
        problems.extend(line_problems)
        plan.append((line_id, quantity))

    if problems:
        # 一次性把**所有**问题说完，而不是第一条就返回：
        # 界面上要能一次标出所有填错的行，不然用户会来回试。
        raise SourcingOrderError("；".join(problems), code="RECEIPT_INVALID")
    return plan


def is_fully_received(plan: Sequence[tuple[str, int]], *, ordered: dict[str, int]) -> bool:
    """整单收货的判定：**每一行都收齐**才算收到。

    第一版只做整单收货，所以主单只有两种结局：收齐 → `RECEIVED`，
    没收齐 → **拒绝**（`receipt_problems` 会把它变成 422，见下）。

    这条判定单独拎出来，是因为它决定了主单状态：
    如果哪天写成"只要收了一行就算收到"，仓库签收一半货单子就进入待付款，
    而付款那边的"未收货不能付款"会被绕过 —— **不报错，只是账做错了**。
    """
    return all(
        # 逐行判"收齐"：这一行收的数量 >= 订单数量，且订单数量本身为正。
        # 订单数量为 0 的行不该出现（`build_order_lines` 已挡掉），
        # 这里再判一次是兜底：万一有历史脏数据，宁可让它算"没收到"，
        # 也不要静默算成收到。
        plan_quantity >= ordered.get(line_id, 0) > 0
        for line_id, plan_quantity in plan
    )


def short_lines(
    plan: Sequence[tuple[str, int]], *, ordered: dict[str, int], names: dict[str, str]
) -> list[str]:
    """没收齐的行，一句行一个人话（`明细「订书机」收 1 / 订 2`）。

    为什么要把"没收齐"说清楚：拒绝部分收货时，用户必须知道**差哪几行、差多少**，
    否则他只能一行一行试 —— 而每次试都是一次被拒的请求。
    """
    messages: list[str] = []
    for line_id, plan_quantity in plan:
        ordered_quantity = ordered.get(line_id, 0)
        if plan_quantity < ordered_quantity:
            label = names.get(line_id, line_id)
            messages.append(
                f"明细「{label}」收 {plan_quantity} / 订 {ordered_quantity}"
            )
    return messages


def receipt_problems(
    plan: Sequence[tuple[str, int]], *, ordered: dict[str, int], names: dict[str, str]
) -> list[str]:
    """整单收货的完整判定：返回**问题列表**（空 = 可以收）。

    ============================ 为什么"部分收货"要**拒绝**而不是"收下但主单不动" ============================
    领域设计把部分收货列进了"第一版先砍掉这些"（第十二节），理由是：
    收了一半之后主单该是什么状态？第一版的状态机里**没有** `PARTIALLY_RECEIVED`
    （`ORDERED → RECEIVED` 是唯一一条边），所以：

        若允许部分收货而主单不动 → 主单停在 ORDERED，但订单上已经写着已收货、
        收货单也已经存在 —— **模型禁止的组合被真实数据造出来了**，
        而且它走不到任何终态（再收一次会被"已经收过货"挡住）。

    于是正确做法是**当场拒绝并说清差哪几行**：用户改完再提交，
    主单与订单一起进 `RECEIVED`。这样"模型允许的状态"与"库里的数据"始终一致 ——
    这也是"砍功能，不砍纪律"的具体样子（实施计划 3144 行）。

    ⚠️ 要恢复部分收货时，按实施计划 5.5 节 E 的四步把 `PARTIALLY_RECEIVED`
    连同转移、主单判定、验收项**一起**加回来，而不是把这里的一句拒绝删掉。
    """
    messages: list[str] = []
    if not plan:
        return ["这张订单没有明细行，无法收货"]
    if not is_fully_received(plan, ordered=ordered):
        messages.append(
            "第一版只支持整单收货，必须每一行都收齐才能登记收货"
        )
        messages.extend(short_lines(plan, ordered=ordered, names=names))
    return messages

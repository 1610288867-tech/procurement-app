"""询价与定标的规则（M10 / 业务规则第 3 条）。**纯函数，不 import 任何框架。**

============================ 这一层回答的三个问题 ============================
1. **一家报价算不算"有效"**？—— `is_effective`：状态对 + 没过期 + 供应商合格。
2. **现在能不能定标**？—— `check_awardable`：返回**问题列表**，不是布尔值。
3. **定标后各家状态变成什么**？—— `award_plan`：选中者 `SELECTED`，其余 `NOT_SELECTED`。

============================ 为什么返回"问题列表"而不是 True/False ============================
定标失败的原因不止一种（家数不够 / 报价过期 / 供应商不合格 / 金额对不上）。
返回布尔值的话，边界层只能编一句"不满足定标条件" —— 而这条信息对采购专员**毫无用处**：
他不知道是"再去谈一家"还是"把过期的那家重新报价"。
问题列表可以直接渲染到界面上，每一条都说清"差什么、差多少"。

这与 M2 的审批策略校验、M6 的建链失败是**同一个模式**：
规则层只负责说清"哪里不对"，怎么呈现是边界层的事。

============================ 4 档状态与"过期"的关系（2026-10-04 定稿）============================
落库状态只有 4 个，**`DRAFT` 被砍掉**：询价是采购专员的工作台动作，
"先存个草稿"这一版没有实际使用场景，留着只会多一条永远走不到的分支
（与状态机里"不留走不到的状态"同一条纪律）。

    SUBMITTED      已提交，等待定标
    EXPIRED        已过期（**由 valid_until 推出来**，见下）
    SELECTED       定标选中
    NOT_SELECTED   定标落选

`EXPIRED` 与另外三个**不是一类**：它随时间自己成立，不靠人操作。
项目里对此已有一条定稿口径（供应商资质过期）：**用查询时过滤实现，不用定时任务** ——
简单，且不会出现"状态与实际不符"的漂移。

但状态既然落库，就必须有人把 `SUBMITTED` 改成 `EXPIRED`，否则
"这家到底是过期了还是还没处理"在库里看着一样。所以这里的做法是
**惰性结算**：读比价表与定标时把"已过有效期却还写着 SUBMITTED"的行改掉
（`expired_ids`）。判定仍然**只认时间**（`valid_until < now`），
状态列只是这份判定的事后投影 —— 投影可以落后一步，判定不会错。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

#: 报价状态（落库字面值，改一个就要写迁移刷历史数据）
QUOTE_SUBMITTED = "SUBMITTED"
QUOTE_EXPIRED = "EXPIRED"
QUOTE_SELECTED = "SELECTED"
QUOTE_NOT_SELECTED = "NOT_SELECTED"

QUOTATION_STATUSES: tuple[str, ...] = (
    QUOTE_SUBMITTED,
    QUOTE_EXPIRED,
    QUOTE_SELECTED,
    QUOTE_NOT_SELECTED,
)

#: 只有这两个状态的报价还能参与定标：
#: `SELECTED` 是"已经定过了"，`NOT_SELECTED` 是"已经落选了" —— 都不该再被算进家数。
#: 少了这条判断，重复定标会把上一次的结果也算成"有效报价"。
BIDDABLE_STATUSES: frozenset[str] = frozenset({QUOTE_SUBMITTED})

#: 比价要求的最少有效报价家数。**业务规则第 3 条写死的数字**，不给配置开关
#: （"至少三家"是制度要求，不是一个可以按环境调的参数）。
MIN_QUOTATIONS = 3


def is_expired(valid_until: datetime | None, *, now: datetime | None = None) -> bool:
    """报价是否已过有效期。

    `valid_until` 为 None 表示**不设有效期**（长期有效）—— 不是"已过期"。
    这一点容易被写反：把 None 当过期，会让所有"没填有效期"的报价永远无法定标，
    而且报错信息会说"报价已过期"，指错方向。

    时间一律按 UTC 比较：库里存的是 `timestamptz`，取出即带时区；
    本函数只做比较，不做本地化（那是展示层的事）。
    """
    if valid_until is None:
        return False
    moment = now or datetime.now(UTC)
    # 库里可能给出 naive 时间（老数据 / 手工插入）：补上 UTC 再比，避免 TypeError
    if valid_until.tzinfo is None:
        valid_until = valid_until.replace(tzinfo=UTC)
    return valid_until < moment


def is_effective(
    *,
    status: str,
    valid_until: datetime | None,
    supplier_usable: bool = True,
    now: datetime | None = None,
) -> bool:
    """一家报价算不算"有效报价"。

    三个条件缺一不可（业务规则第 3 条的四条硬控制里占了三条）：

        状态可投标（SUBMITTED）
        没过有效期
        供应商本身合格（未停用、资质未过期 —— 由调用方查好传进来）

    供应商那一项**不在这里查库**：本层是纯函数，`supplier_usable` 由调用方
    （协调者，拿着供应商仓库）算好传进来。这样"合格"的判据仍然只有一处
    （`CatalogRepository` 的查询），不会在这里长出第二份会漂移的定义。
    """
    return (
        status in BIDDABLE_STATUSES
        and not is_expired(valid_until, now=now)
        and supplier_usable
    )


@dataclass(frozen=True)
class QuotationFacts:
    """定标判定需要的事实（**不是 ORM 记录**）。

    只带规则用得上的字段：这样纯函数测试不用造数据库对象，
    协调者从记录里挑几个字段塞进来即可。
    """

    supplier_id: str
    amount_fen: int
    status: str
    valid_until: datetime | None
    #: 供应商是否合格（未停用 + 资质未过期）。由调用方查好。
    supplier_usable: bool = True


def check_awardable(
    quotations: Sequence[QuotationFacts],
    *,
    amount_fen: int,
    threshold_fen: int,
    now: datetime | None = None,
) -> list[str]:
    """能不能定标。返回**问题列表**，空列表 = 可以。

    ============================ 比价线两侧是两条不同的规则 ============================
        金额 <  比价线  → **不要求**家数（一家报价也能定：小额直采不必凑三家）
        金额 >= 比价线  → **至少 MIN_QUOTATIONS 家**有效报价

    边界取"闭区间"（`>=`）：正好等于比价线就按"超过"处理。
    这种取法要写死在一处并测到 —— 否则"刚好 10000 元要不要比价"会在
    开发、测试、制度解释三处得到不同答案。

    报错信息要**说清差多少**（"当前 2 家，还差 1 家"），不是"不满足条件"。
    """
    problems: list[str] = []
    effective = [
        item
        for item in quotations
        if is_effective(
            status=item.status,
            valid_until=item.valid_until,
            supplier_usable=item.supplier_usable,
            now=now,
        )
    ]

    if amount_fen >= threshold_fen and len(effective) < MIN_QUOTATIONS:
        short = MIN_QUOTATIONS - len(effective)
        problems.append(
            f"金额超过比价线，至少需要 {MIN_QUOTATIONS} 家有效报价，"
            f"当前 {len(effective)} 家（还差 {short} 家）"
        )
    elif not effective:
        # 金额**低于**比价线时上面那条不会触发，这条才是唯一提示：
        # 不管金额多少，一家有效报价都没有就没法定标（定标要有对象）。
        problems.append("没有任何有效报价，无法定标")

    return problems


def expired_ids(
    quotations: Sequence[QuotationFacts],
    *,
    now: datetime | None = None,
) -> list[int]:
    """哪些报价"该被标成 EXPIRED"（下标）。

    ============================ 为什么返回下标而不是 id ============================
    本层不认识数据库主键（`QuotationFacts` 里只有规则用得上的字段）。
    调用方拿下标去对应自己那份记录列表 —— 顺序是调用方给的，它自己最清楚。

    只挑**还写着 SUBMITTED 但已过期**的：已经 SELECTED / NOT_SELECTED 的是
    "定标那一刻的历史结论"，不该因为时间流逝被改写
    （否则落选的报价过一阵会变成"过期"，看着像是它自己失效的）。
    """
    return [
        index
        for index, item in enumerate(quotations)
        if item.status == QUOTE_SUBMITTED and is_expired(item.valid_until, now=now)
    ]


def award_plan(
    quotations: Sequence[QuotationFacts], *, selected_supplier_id: str
) -> list[tuple[int, str]]:
    """定标计划：返回 `[(下标, 新状态), ...]`。

    **选中者 `SELECTED`，其余一律 `NOT_SELECTED`** —— 包括那些没参与比价的
    （过期 / 落选的）：定标是一个"这一轮结束了"的结论，其余报价不该还留在
    `SUBMITTED` 上，否则下一轮询价会把它们算成有效家数。

    只给状态、不改金额：**成交单价取选中报价的原值**，
    不允许在定标这一步顺手改价（改价要走"重新报价"，那才有痕迹）。
    """
    plan: list[tuple[int, str]] = []
    for index, item in enumerate(quotations):
        if item.supplier_id == selected_supplier_id:
            plan.append((index, QUOTE_SELECTED))
        else:
            plan.append((index, QUOTE_NOT_SELECTED))
    return plan


def deal_unit_price_fen(quotations: Sequence[QuotationFacts], *, supplier_id: str) -> int | None:
    """取某家供应商的**成交单价**（分）。

    ============================ 为什么单独一个函数 ============================
    "订单金额以订单明细为准、单价取定标报价"是本项目**定死过的口径**（ER 图第 136-142 行）：
    订单总额通常**小于**申请总额，差额由预算的"实付小于预留则显式释放"处理。

    所以"成交单价"这件事必须只有一个入口：写错一次（例如误用申请单价），
    订单总额就悄悄变回申请金额，而**三单匹配会跟着一起错**——
    发票金额与订单金额比，两者都错就抵消了，谁也不会报错。
    """
    for item in quotations:
        if item.supplier_id == supplier_id:
            return item.amount_fen
    return None


def usable_supplier_ids(quotations: Iterable[QuotationFacts]) -> set[str]:
    """合格供应商的 id 集合（给"同一家不许报两次"之类的校验用）。"""
    return {item.supplier_id for item in quotations if item.supplier_usable}

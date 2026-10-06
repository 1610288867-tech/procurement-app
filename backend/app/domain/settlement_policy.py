"""三单匹配（`domain/settlement_policy.py`）：订单 / 收货 / 发票对账。**纯函数，不 import 任何框架。**

第一版按**订单级**粒度（领域设计 A.5 + 实施计划 2412-2423）：
先把每张单的明细**求和**，再拿三个标量去比 —— 因为 `match_three_way` 的入参全是标量，
而表结构里订单有明细行、收货有多行、发票可以有多张，**两边的粒度对不上**。
明细行级匹配（能发现"甲行多收、乙行少收"）进"暂时砍掉"清单，M10 之后按需。

============================ 返回"问题列表"而不是布尔值 ============================
布尔值只能告诉用户"失败了"，问题列表能告诉他"哪里不对、差多少"，
前端拿它直接渲染三列对比页的差异高亮：

    采购订单          收货单           发票
    数量 100          实收 100          —
    金额 ¥12,000      —                金额 ¥12,300   ← 差异 +2.5%，超出容差
    供应商 甲          —                供应商 甲  ✓
                                        ↑ 高亮这一行

**第一版做"拒绝 + 返回问题列表"，不做"挂起 + 异常任务"。**

============================ 容差：比例与绝对值**取较大者** ============================
    tolerance = max(订单金额 × 2%, 50 元)

为什么取较大者：小额单（比如 200 元）的 2% 只有 4 元，
而实际开票有个几分几毛的零头是常态 —— 只按比例算，**小额单永远匹配不上**。

============================ 为什么用"万分比整数"而不是 0.02 ============================
项目硬约束第 3 条：**金额一律整数分，禁止 float**。写 `int(amount * 0.02)` 会引入
浮点误差，而这条误差最后会体现成"差 1 分钱到底算不算超容差"这种没法解释的边界
（`0.02 * 2500 = 50.000000000000007`，一个边界用例就能让它翻车）。
所以比例存成**万分比整数** `TOLERANCE_BP = 200`（= 2%），乘法与整除全程整数。
下面的 `test_*` 里专门有一条钉住这个边界。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 容差比例：**万分比**（200 bp = 2%）。用整数是为了不引入浮点误差。
TOLERANCE_BP = 200
#: 容差下限：50 元 = 5000 分。与比例**取较大者**。
TOLERANCE_ABS_FEN = 5_000

#: 问题码（前端按它决定高亮哪一列）
ISSUE_OVER_RECEIPT = "OVER_RECEIPT"
ISSUE_AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
ISSUE_SUPPLIER_MISMATCH = "SUPPLIER_MISMATCH"
#: 结构性问题：压根没有订单 / 没有发票。它们不属于"三单对不上"，
#: 而是"还没到能对账的地步" —— 但预检页同样要显示，所以也进问题列表。
ISSUE_NO_ORDER = "NO_ORDER"
ISSUE_NO_INVOICE = "NO_INVOICE"

# ---------------- 发票状态（M10 / 8.3 补：作废） ----------------
#: 有效发票：参与三单匹配的金额求和。
INVOICE_ACTIVE = "ACTIVE"
#: 作废发票：**不参与求和**，且它的发票号可以重新登记。
#: 用 `VOIDED` 而不是 `CANCELLED`：发票是**外部凭证**，
#: 我们无权"取消"它 —— 能做的只是把它从自己的账里作废掉（原件仍在供应商那边）。
INVOICE_VOIDED = "VOIDED"
INVOICE_STATUSES: tuple[str, ...] = (INVOICE_ACTIVE, INVOICE_VOIDED)


class InvoiceError(ValueError):
    """发票规则不满足。带 `code` 供边界层转成统一错误体。"""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def can_void_invoice(*, status: str, request_status: str) -> None:
    """能不能作废这张发票。不能就抛 `InvoiceError`。

    ============================ 两条拒绝，各自的理由不同 ============================
        已经是 `VOIDED`  → 重复作废（不是错误操作，但不该被当成成功 ——
                          否则用户会以为"又作废了一次"，而痕迹只有一条）
        主单已过付款      → **不许作废**：钱已经按这张票付过了。
                          这一条是本项目里少见的"宁可让人人工处理"的分支 ——
                          作废一张已付款的票会让"已付金额 vs 有效发票合计"
                          在账上对不上，而系统没有退款流程。

    主单状态用**字符串**传进来（而不是枚举）：`domain/` 层的判定不依赖枚举类，
    这样它既能被接口层调、也能被将来的批处理调，测试里也不必构造枚举。
    """
    if status == INVOICE_VOIDED:
        raise InvoiceError("这张发票已经作废过了", code="INVOICE_ALREADY_VOIDED")
    if request_status in _PAID_OR_LATER:
        raise InvoiceError(
            "这张单子已经付过款了，不能再作废发票"
            "（作废会让已付金额与有效发票合计对不上，且系统没有退款流程；"
            "请人工处理并留书面记录）",
            code="INVOICE_VOID_AFTER_PAYMENT",
        )


#: 付款之后的主单状态。用模块级常量集合，避免在判定里散落字符串字面量。
#: ⚠️ 8.4 加上 `PAID` / `CLOSED` 时**只改这一处** —— 判定的调用方不用动。
_PAID_OR_LATER = frozenset({"PAID", "CLOSED"})


def voidable(invoice_statuses: list[str]) -> bool:
    """这三单里**是否还有有效发票**（作废之后一张不剩 → 匹配要报 `NO_INVOICE`）。

    单独一个函数，是因为"作废之后还算不算'有发票'"这件事很容易写错：
    拿 `len(invoices) > 0` 去判，会得到"登记过 3 张、全作废了"仍然算有发票 ——
    于是匹配会拿着 0 元去和订单金额比，报出"金额差超容差"，
    而真正的原因是**一张有效发票都没有**（该报 `NO_INVOICE`）。
    """
    return any(status == INVOICE_ACTIVE for status in invoice_statuses)


@dataclass(frozen=True)
class MatchIssue:
    """一条不匹配。`expected` / `actual` 只在有可比数字时给（如供应商不一致就没得比）。"""

    code: str
    message: str
    expected: int | None = None
    actual: int | None = None


def tolerance_fen(ordered_amount_fen: int) -> int:
    """容差（分）= max(订单金额 × 2%, 50 元)。

    全程整数：`ordered_amount_fen * TOLERANCE_BP // 10_000` 是**向下取整**，
    也就是容差**只可能偏小、不可能偏大** —— 兜底方向选严的那一侧
    （与"权限兜底往窄里兜"同一条纪律：宁可多拦一次，也不要放过一次）。
    """
    return max(ordered_amount_fen * TOLERANCE_BP // 10_000, TOLERANCE_ABS_FEN)


def match_three_way(
    *,
    ordered_qty: int,
    ordered_amount_fen: int,
    order_supplier_id: str,
    accepted_qty: int,
    invoice_amount_fen: int,
    invoice_supplier_id: str,
) -> list[MatchIssue]:
    """返回所有不匹配项。**空列表表示三单匹配通过，可以付款。**

    三条规则（业务规则第 6 条）：

        ① 收货数量 <= 订单数量（超收要人工确认）
        ② |发票金额 - 订单金额| <= 容差
        ③ 发票供应商 == 订单供应商

    ⚠️ **金额基准是"订单金额"，不是"申请金额"。**
    询价压价之后订单总额通常小于申请总额，拿申请金额来比会一律判成不匹配，
    而差额其实由 M7 的"实付小于预留则显式释放"处理（业务规则 178-182 行）。
    """
    issues: list[MatchIssue] = []

    if accepted_qty > ordered_qty:
        issues.append(
            MatchIssue(
                code=ISSUE_OVER_RECEIPT,
                message=f"实收数量 {accepted_qty} 超过订单数量 {ordered_qty}",
                expected=ordered_qty,
                actual=accepted_qty,
            )
        )

    allowed = tolerance_fen(ordered_amount_fen)
    difference = abs(invoice_amount_fen - ordered_amount_fen)
    if difference > allowed:
        issues.append(
            MatchIssue(
                code=ISSUE_AMOUNT_MISMATCH,
                message=(
                    f"发票金额 {invoice_amount_fen} 分与订单金额 {ordered_amount_fen} 分"
                    f"相差 {difference} 分，超出容差 {allowed} 分"
                ),
                expected=ordered_amount_fen,
                actual=invoice_amount_fen,
            )
        )

    if invoice_supplier_id != order_supplier_id:
        issues.append(
            MatchIssue(
                code=ISSUE_SUPPLIER_MISMATCH,
                message="发票供应商与订单供应商不一致",
            )
        )

    return issues


def precheck_three_way(
    *,
    ordered_qty: int | None,
    ordered_amount_fen: int | None,
    order_supplier_id: str | None,
    accepted_qty: int | None,
    invoice_amount_fen: int,
    invoice_count: int,
    all_suppliers_match: bool,
) -> list[MatchIssue]:
    """预检：先判"够不够格对账"，再跑 `match_three_way`。

    ============================ 为什么单独一层 ============================
    A.5 的 `match_three_way` 是**整单对整单**的标量比较，它没法表达
    "压根还没有订单""一张有效发票都没有"这类情况（传 0 进去会被算成
    "金额差超容差"，而真正的原因是**还没到那一步**）。所以这一层负责结构性的判断，
    判定规则本身仍然只有 A.5 那一份实现。

    ⚠️ `invoice_count` 必须是**有效（未作废）发票的张数**，不是登记的条数：
    全部作废之后应当报 `NO_INVOICE`，而不是"发票金额 0 与订单金额差超容差"。
    调用方用 `voidable(...)` 或按状态过滤后传进来。

    ============================ 多张发票的供应商一致性 ============================
    领域设计 1372 行专门点了这条：**多张发票时必须先校验"全部发票的供应商都等于订单
    供应商"，再传一个标量进来** —— 否则签名里的 `invoice_supplier_id` 根本表达不了
    "三张发票里有一张是别家"这种情况。所以入参是 `all_suppliers_match: bool`
    （由调用方对**所有**发票求"与"），而不是某一张发票的供应商 id。
    """
    issues: list[MatchIssue] = []

    if ordered_qty is None or ordered_amount_fen is None or order_supplier_id is None:
        # 没有订单就没法对账（正常链路里 RECEIVED 一定有订单，
        # 但预检是只读接口，读到一个中间状态不该 500）
        issues.append(
            MatchIssue(code=ISSUE_NO_ORDER, message="这张申请还没有采购订单，无法三单匹配")
        )
        return issues

    if invoice_count == 0:
        # 一张有效发票都没有：可能是"还没登记"，也可能是"登记的都作废了"。
        # 两种情况都归到这里 —— 因为它们对匹配的意义相同：**没有可对账的金额**。
        issues.append(
            MatchIssue(
                code=ISSUE_NO_INVOICE,
                message="这张申请没有有效发票（未登记，或已登记的都作废了），无法三单匹配",
            )
        )
        return issues

    issues.extend(
        match_three_way(
            ordered_qty=ordered_qty,
            ordered_amount_fen=ordered_amount_fen,
            order_supplier_id=order_supplier_id,
            accepted_qty=accepted_qty if accepted_qty is not None else 0,
            invoice_amount_fen=invoice_amount_fen,
            # 全部一致才传订单供应商；有一张不一致就传一个**不可能相等**的值，
            # 让 A.5 那条规则统一报出来（判定只有一处实现）
            invoice_supplier_id=(
                order_supplier_id if all_suppliers_match else f"!={order_supplier_id}"
            ),
        )
    )
    return issues


def document_supplier_consistent(*, order_supplier_id: str, supplier_ids: list[str]) -> bool:
    """所有发票的供应商是否**都**等于订单供应商。

    单独一个函数，因为"求与"这件事很容易被写成"看第一张"：
    `supplier_ids[0] == order_supplier_id` 在三张发票里只有第二张是别家时会通过，
    而那张恰恰是要拦下来的。空列表返回 **False**（没有发票就谈不上一致）。
    """
    if not supplier_ids:
        return False
    return all(supplier_id == order_supplier_id for supplier_id in supplier_ids)

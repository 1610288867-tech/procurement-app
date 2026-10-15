"""成本核算（M11 增强）：token 用量 → 费用。纯函数，不 import 任何框架。

============================ 为什么这件事值得单独一个函数 ============================
"这个功能一个月花多少钱"是 AI 应用落地**必须能回答**的问题，
而它最容易出错的地方不是乘法，是**口径**：

    · token 用量拿不到时，能不能用字符数估算？—— **不能**（见下）
    · 本地模型（自己跑）该记 0 还是留空？—— **记 0**，因为"确实不花钱"与"不知道花多少"不同
    · 单价按"每 100 万 token"还是"每 1000"？—— 统一到**微元**，避免浮点

============================ 硬纪律：拿不到用量就留空，不要估算 ============================
一个字符 ≈ 0.6 token 这类估算法看着方便，但它会把**估算值混进真实用量**：
月底一看"花了 12 元"，其中多少是真的、多少是估的，没法分开。
而成本核算的意义恰恰在于**能对账**。所以：
`estimate_cost_micros` 在缺任一必需字段时返回 `None`（不知道），**不猜**。

============================ 为什么用"微元"而不是"元（浮点）" ============================
与金额用"分"是同一条纪律：浮点在累加几千次之后会有肉眼可见的误差，
而且 `0.1 + 0.2 != 0.3` 会让"两次调用加起来等于一次吗"这种比较变得不可靠。
微元（1e-6 元）在整数域里有足够精度：最小的常见单价
（0.5 元/百万 token = 0.5 微元/token）也能精确表达。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 1 元 = 1_000_000 微元
MICROS_PER_YUAN = 1_000_000
#: 单价按"每 100 万 token"给出（行业惯例的报价单位）
TOKENS_PER_PRICE_UNIT = 1_000_000


@dataclass(frozen=True)
class ModelPrice:
    """模型单价，单位：**元 / 百万 token**。

    用 `Decimal` 级的精确整数表达：价格以"微元/百万 token"为整数存，
    避免 `0.5` 这种浮点字面量进到计算里。
    """

    #: 输入价格（微元 / 百万 token）
    input_micros_per_million: int
    #: 输出价格（微元 / 百万 token）
    output_micros_per_million: int

    @staticmethod
    def from_yuan_per_million(*, input_yuan: float, output_yuan: float) -> ModelPrice:
        """从"元/百万 token"构造（给配置用的便捷入口）。

        内部立刻转成整数微元，**构造之后就不再有浮点**。
        """
        return ModelPrice(
            input_micros_per_million=round(input_yuan * MICROS_PER_YUAN),
            output_micros_per_million=round(output_yuan * MICROS_PER_YUAN),
        )


def estimate_cost_micros(
    *,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    price: ModelPrice | None,
) -> int | None:
    """算一次调用的成本（微元）。**任一必需信息缺失就返回 `None`（不知道）。**

    返回 `None` 的三种情况，各自的含义不同，调用方要能区分：
        · `prompt_tokens` 或 `completion_tokens` 拿不到  → 用量未知
        · `price` 为 `None`（模型不在价格表里）        → 单价未知
        · 两者都缺

    ⚠️ **本地模型不是这三种情况**：它跑在自己的机器上，
    费用**确实是 0**。所以调用方应当给它一个 `ModelPrice(0, 0)` 而不是 `None` ——
    这样"不花钱"与"不知道花多少"在数据里是两回事。
    """
    if prompt_tokens is None or completion_tokens is None or price is None:
        return None
    if prompt_tokens < 0 or completion_tokens < 0:
        # 上游给了负数说明它的 usage 字段不可信 —— 宁可不记，也不要记一个负数
        return None
    total = (
        prompt_tokens * price.input_micros_per_million
        + completion_tokens * price.output_micros_per_million
    )
    # 单价是"每百万 token"，所以最后除以一百万。
    # 用整数除法是**故意的**：宁可少算不到 1 微元，也不要引入浮点。
    return total // TOKENS_PER_PRICE_UNIT


def format_cost(micros: int | None) -> str:
    """微元 → 给人看的字符串（报告与日志用）。`None` 打 `未知`。

    `未知` 与 `¥0.000000` 必须长得不一样 —— 前者是"没量到"，后者是"确实免费"。
    """
    if micros is None:
        return "未知"
    return f"¥{micros / MICROS_PER_YUAN:.6f}"


def total_cost_micros(rows: list[int | None]) -> tuple[int, int]:
    """汇总一批记录的成本，返回 `(已知总额, 用量未知的条数)`。

    为什么要**同时**返回"未知条数"：只报一个总额会让人以为它是全部费用，
    而实际上可能有一半调用因为拿不到 usage 根本没算进去。
    **一个不说明覆盖率的成本数字，比没有数字更危险。**
    """
    known = [row for row in rows if row is not None]
    unknown = len(rows) - len(known)
    return sum(known), unknown

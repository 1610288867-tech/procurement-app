"""模型价格表的解析（M11 增强）。纯函数，不 import 任何框架。

============================ 为什么价格表是"配置字符串"而不是结构化配置 ============================
项目的纪律是"**配置项保持个位数**"，而价格表天然是"每个模型一条"——
把它做成结构化配置会让配置项数量随模型数增长。
所以用一个 `模型名=输入价,输出价;模型名=...` 的字符串，**规则**放在这里做。
这与"审批策略是 JSON 数据、规则在领域层"是同一个思路。

============================ 解析必须"错就报出来"，不能静默丢弃 ============================
一条写错的价格（比如 `qwen2.5:7b=abc,def`）如果被静默跳过，
后果是**该模型的调用全部记成"成本未知"**，而没人知道为什么。
所以这里返回 `(价格表, 问题列表)`：**能解析的照常解析，解析不了的报出来**。
既不让一条坏配置毁掉全表，也不让它无声地消失。
"""

from __future__ import annotations

from app.domain.cost import ModelPrice


def parse_model_prices(raw: str) -> tuple[dict[str, ModelPrice], list[str]]:
    """解析价格表配置。返回 `(模型名 → 单价, 问题列表)`。

    格式：`模型名=输入价,输出价`，多条用 `;` 或换行分隔。空白项忽略。
    价格单位是**元 / 百万 token**（行业惯例的报价单位）。

    ⚠️ **模型名里可能含冒号**（`qwen2.5:7b`）—— 所以用 `=` 而不是 `:` 分隔，
    而且只在**第一个** `=` 处切分（模型名本身不会含 `=`）。
    """
    prices: dict[str, ModelPrice] = {}
    problems: list[str] = []

    for chunk in raw.replace("\n", ";").split(";"):
        entry = chunk.strip()
        if not entry:
            continue
        if "=" not in entry:
            problems.append(f"价格条目缺少 `=`：{entry!r}（应为 `模型名=输入价,输出价`）")
            continue
        name, _, values = entry.partition("=")
        name = name.strip()
        parts = [part.strip() for part in values.split(",")]
        if not name or len(parts) != 2:
            problems.append(f"价格条目格式不对：{entry!r}（应为 `模型名=输入价,输出价`）")
            continue
        try:
            input_yuan = float(parts[0])
            output_yuan = float(parts[1])
        except ValueError:
            problems.append(f"价格不是数字：{entry!r}")
            continue
        if input_yuan < 0 or output_yuan < 0:
            problems.append(f"价格不能为负：{entry!r}")
            continue
        prices[name] = ModelPrice.from_yuan_per_million(
            input_yuan=input_yuan, output_yuan=output_yuan
        )

    return prices, problems

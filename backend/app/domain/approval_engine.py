"""审批链解析（M6）—— 整个系统的核心。

这一层是纯 Python，**不 import 任何框架**，也不碰数据库：
输入是"品类的审批策略 + 这条申请的上下文"，输出是"要走哪几级、每级谁审"。

============================ 为什么它是纯函数 ============================
"按金额动态生成审批链"是本项目最值钱的那件事，而它**天然是纯逻辑**：
没有并发、没有 IO、没有时间。做成纯函数之后：

  - 单测是毫秒级的，改一行立刻知道对错；
  - 那张《金额→审批链》对照表可以直接当参数化测试的输入（500/5000/50000/200000 → 1/2/4/5）；
  - 将来换框架、换数据库，这个文件一行不用改。

============================ 输出里为什么带 source ============================
`POLICY` 是策略里本来就有的那一级；`URGENCY` 是"加急件临时追加"的那一级。
两者的**依据不同**，事后解释"这单为什么多了一级"时要能区分：
是规则本来就这么配的，还是因为标了加急。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.domain.rules import RuleError, evaluate

#: 探测金额（单位：分）：0 / 1 元 / 10 元 / 100 元 / 1000 元 / 10000 元 / 100000 元。
#: 用它反推"每个步骤是否至少被触发过一次、有没有金额算出空链"。
PROBE_AMOUNTS_FEN: tuple[int, ...] = (0, 100, 1_000, 10_000, 100_000, 1_000_000, 10_000_000)

#: 加急追加级的默认 order。策略里没写 order 时用它 ——
#: 取 5 是"排在最前面"，因为加急件的第一级本来就该是加急审批人（如部门负责人）。
DEFAULT_URGENCY_ORDER = 5


@dataclass(frozen=True)
class ApprovalStep:
    """审批链上的一级。"""

    order: int
    role: str
    mode: str  # SINGLE（或签） / ALL（会签）
    source: str  # POLICY / URGENCY


# ============================================================ 职责分离（SoD）
#
# 这一组是 2026-10-03 补的。起因：实跑发现**申请人能批掉自己的申请**。
# 依据（原文见 docs/采购内控对照.md）：
#   - 财政部《企业内部控制应用指引第 7 号》第五条：
#     "企业除小额零星物资或服务外，不得安排同一机构办理采购业务全过程"；
#   - 上市公司制度范本的**不相容岗位**第一条：**请购与审批**必须分离；
#   - Oracle Procurement 的 **Prohibit User Self-Approval**：把提交人从审批链里 skip 掉；
#   - OWASP：最小权限、默认拒绝，客户端校验不能作为放行依据。
#
# 为什么做成"纯函数"：这个判定**错了不会报错**，只会让内控静默失效（单据照样走完）。
# 外部实现（human-review-console）也是把它做成 fail-closed 的纯函数判定。
# 只有纯函数才测得动、测得快。


def is_eligible_approver(candidate_id: Any, requester_id: Any) -> bool:
    """这个人能不能当这条申请的审批人。

    **唯一规则**：提交人不能审自己的申请（fail-closed —— 认不出就判不合格）。
    """
    return candidate_id is not None and candidate_id != requester_id


def exclude_requester(candidates: Sequence[Any], requester_id: Any) -> list[Any]:
    """把提交人从候选人里剔除，其余保持原顺序。

    与"同一角色去重"是两件不同的事：
      - 去重解决的是"同一个人在同一张链上出现两次"；
      - 这里解决的是"提交人自己就是这一级的审批人"。
    只做前者，申请人照样能批掉自己的单（M6 就是这么漏的）。
    """
    return [item for item in candidates if is_eligible_approver(item.id, requester_id)]


def resolve_approval_chain(
    policy: Mapping[str, Any],
    request_context: Mapping[str, Any],
) -> list[ApprovalStep]:
    """算出这条申请要走哪几级审批。

    四步，顺序不能换：

      1. 逐条评估 `steps` 里的 `when`，为真的入链；
      2. 加急件追加 `urgency_extra_step`（如果有，且条件成立）；
      3. 走框架协议的品类**去掉采购专员那一级**（框架协议已经定好供应商与价格，
         再让采购审一次是重复劳动）；
      4. 按 `order` 排序，**同一角色只保留最靠前的一级**。

    第 4 步容易被忽略但很关键：策略里同一个角色写两次（比如部门主管在 10 和 30 各有一条），
    不去重的话同一个人会在待办里看到两条一模一样的审批。
    """
    steps: list[ApprovalStep] = []

    raw_steps = policy.get("steps")
    if isinstance(raw_steps, (list, tuple)):
        for item in raw_steps:
            if not isinstance(item, Mapping):
                continue
            when = item.get("when")
            if when is None:
                continue
            try:
                matched = evaluate(when, request_context)
            except RuleError:
                # 策略本身非法（结构错）时**不在这里炸**：保存策略时已经被
                # `validate_policy` 拦过一次；这里遇到脏数据就当"这一级不满足"，
                # 免得一条坏规则让整张单子提交不了（而且错误信息离根因很远）。
                matched = False
            if matched:
                steps.append(
                    ApprovalStep(
                        order=int(item.get("order", 0)),
                        role=str(item.get("role", "")),
                        mode=str(item.get("mode", "SINGLE")),
                        source="POLICY",
                    )
                )

    urgency = policy.get("urgency_extra_step")
    if isinstance(urgency, Mapping) and urgency.get("when") is not None:
        try:
            urgent_matched = evaluate(urgency["when"], request_context)
        except RuleError:
            urgent_matched = False
        if urgent_matched:
            steps.append(
                ApprovalStep(
                    order=int(urgency.get("order", DEFAULT_URGENCY_ORDER)),
                    role=str(urgency.get("role", "")),
                    mode=str(urgency.get("mode", "SINGLE")),
                    source="URGENCY",
                )
            )

    # 走框架协议：跳过询价环节，因此不需要采购专员审批
    if policy.get("skip_when_framework_contract") and request_context.get(
        "framework_contract"
    ):
        steps = [step for step in steps if step.role != "PROCUREMENT"]

    steps.sort(key=lambda step: step.order)

    seen: set[str] = set()
    unique: list[ApprovalStep] = []
    for step in steps:
        if not step.role or step.role in seen:
            continue
        seen.add(step.role)
        unique.append(step)
    return unique


def check_reachability(
    policy: Mapping[str, Any], *, urgency: str | None = None
) -> list[str]:
    """第四层（最外层）校验：拿几个代表性金额反推策略是否"处处可达"。

    查出三类问题，都是**不会报错、只会算错**的那种：

      1. **空审批链**：某个金额区间算出来一级都没有 = 无人审批直接通过；
      2. **死步骤**：某一级在任何探测金额下都不会被触发 —— 管理员以为配了，
         实际上永远不会生效，等于审批层级**静默降低**；
      3. **加急级永远不生效**：`urgency_extra_step` 的条件在加急件上都不成立。

    `urgency` 传进来时会额外探一次加急上下文，免得"只在加急件上生效的那一级"
    被误判成死步骤（策略 JSON 里那个条件正好是 `urgency == "URGENT"`）。

    返回问题列表（空 = 通过），调用方拿到非空列表应该直接 422。
    """
    problems: list[str] = []

    triggered: set[int] = set()
    for amount in PROBE_AMOUNTS_FEN:
        try:
            chain = resolve_approval_chain(policy, {"amount_fen": amount})
        except RuleError as exc:  # pragma: no cover - 结构错误在校验前就该被拦
            problems.append(f"金额 {amount} 分评估失败：{exc}")
            continue
        if not chain:
            problems.append(
                f"金额 {amount} 分（{amount / 100:.2f} 元）算出的审批链为空（无人审批）"
            )
        triggered |= {step.order for step in chain}

    steps: Sequence[Any] = policy.get("steps") or ()
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        order = step.get("order")
        if order is not None and int(order) not in triggered:
            problems.append(
                f"order={order} 在任何探测金额下都不会被触发（这一级配了但永远不生效）"
            )

    # 加急追加级：只在加急上下文里探一次，否则会被误判成死步骤
    urgency_step = policy.get("urgency_extra_step")
    if isinstance(urgency_step, Mapping) and urgency is not None:
        try:
            urgent_chain = resolve_approval_chain(
                policy, {"amount_fen": PROBE_AMOUNTS_FEN[-1], "urgency": urgency}
            )
        except RuleError:  # pragma: no cover
            urgent_chain = []
        if not any(step.order == int(urgency_step.get("order", DEFAULT_URGENCY_ORDER)) for step in urgent_chain):
            problems.append(
                "urgency_extra_step 在加急件上也不会被触发（这一级配了但永远不生效）"
            )

    return problems

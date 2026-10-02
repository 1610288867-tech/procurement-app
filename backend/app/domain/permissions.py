"""数据级权限：按角色算「可见范围」（M3）。

这一层是纯 Python，**不 import 任何框架** —— 它只回答"该看到多宽"，
不负责生成 SQL。真正把它拼进 `WHERE` 的适配器在基础设施层（M4 落地）。

============================ 为什么单独一个模块 ============================
角色校验只解决"能不能用这个接口"；它回答不了"能看到哪些行"。
后者才是采购系统里最容易出事的地方，而且**漏了不会报错**——
只是安安静静地把别人的申请也查了出来。

所以只留一个判定入口：谁要查数据都得先经过它，然后**把结果写进 WHERE**，
而不是"先查出来再判断能不能看"。

============================ 兜底方向 ============================
认不出的角色一律按**最窄**的范围处理。权限兜底只能往小里兜：
往大里兜一次，就是一次越权。
"""

from collections.abc import Iterable
from enum import IntEnum

# 8 个角色（GM 由 ADMIN 兼任，所以初始化数据里是 7 个人）
ROLE_REQUESTER = "REQUESTER"
ROLE_DEPT_MANAGER = "DEPT_MANAGER"
ROLE_DEPT_HEAD = "DEPT_HEAD"
ROLE_PROCUREMENT = "PROCUREMENT"
ROLE_FINANCE = "FINANCE"
ROLE_GM = "GM"
ROLE_RECEIVER = "RECEIVER"
ROLE_ADMIN = "ADMIN"

ALL_ROLES: frozenset[str] = frozenset(
    {
        ROLE_REQUESTER,
        ROLE_DEPT_MANAGER,
        ROLE_DEPT_HEAD,
        ROLE_PROCUREMENT,
        ROLE_FINANCE,
        ROLE_GM,
        ROLE_RECEIVER,
        ROLE_ADMIN,
    }
)


class Visibility(IntEnum):
    """可见范围。

    **顺序是逻辑的一部分**：取最宽的那个靠比较大小，别把顺序改反
    （改反了部门主管就会看到全公司）。
    """

    SELF = 1  # 只看自己提的
    DEPARTMENT = 2  # 本部门（含子部门）
    ALL = 3  # 跨部门看全公司


# 职责本身就是跨部门的角色。GM 看的是"金额重大的单子"，不分部门。
_COMPANY_WIDE = frozenset(
    {ROLE_PROCUREMENT, ROLE_FINANCE, ROLE_RECEIVER, ROLE_GM, ROLE_ADMIN}
)
# 只在本部门（含子部门）内可见的审批角色。
_DEPARTMENT_WIDE = frozenset({ROLE_DEPT_MANAGER, ROLE_DEPT_HEAD})


def resolve_visibility(roles: Iterable[str]) -> Visibility:
    """一个用户可能持多个角色（如 ADMIN 兼 GM），取**最宽**的那个。"""
    held = {role for role in roles if role in ALL_ROLES}
    if held & _COMPANY_WIDE:
        return Visibility.ALL
    if held & _DEPARTMENT_WIDE:
        return Visibility.DEPARTMENT
    return Visibility.SELF

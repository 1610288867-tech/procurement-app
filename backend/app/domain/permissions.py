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

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
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


@dataclass(frozen=True)
class RequestScope:
    """"这个用户能看哪些采购申请"的判定结果（M5）。

    它是**给仓库层用的数据**，不是 SQL：这里只回答"按什么过滤"，
    拼 `WHERE` 是基础设施层的事（`domain/` 不许出现 SQL）。

    ============================ 为什么必须是个"算出来的对象" ============================
    权限条件一定要**先算出来、再进 WHERE**。写成"先查出来再判断能不能看"的话，
    漏一处就是越权，而且**不会报错** —— 查询照样成功，只是多返回了几行别人的数据。

    所以仓库的每一个查询方法都必须收下这个对象，没有"这次先不过滤"的口子。
    """

    #: 全部可见（跨部门角色）
    sees_all: bool = False
    #: 只按申请人过滤（SELF）
    only_self: bool = False
    #: DEPARTMENT：申请人所在部门 + 子部门
    department_ids: frozenset[uuid.UUID] = frozenset()

    def __post_init__(self) -> None:
        # 两个"全开"的开关不能同时为真：那会让调用方以为自己在过滤，其实没过滤
        if self.sees_all and self.only_self:
            raise ValueError("RequestScope 不能同时 sees_all 与 only_self")


def resolve_request_scope(
    roles: Iterable[str],
    *,
    actor_id: uuid.UUID,
    department_id: uuid.UUID | None,
    sub_department_ids: Iterable[uuid.UUID] = (),
) -> RequestScope:
    """把角色翻译成采购申请的可见范围。

    `sub_department_ids` 由调用方从部门树查出来（这一层不碰数据库）。
    没有部门的用户（如系统管理员）如果角色窄，就只能看自己的 ——
    **兜底方向只能往小里兜**：往大里兜一次，就是一次越权。
    """
    visibility = resolve_visibility(roles)
    if visibility is Visibility.ALL:
        return RequestScope(sees_all=True)
    if visibility is Visibility.DEPARTMENT and department_id is not None:
        return RequestScope(
            department_ids=frozenset({department_id, *sub_department_ids})
        )
    return RequestScope(only_self=True)

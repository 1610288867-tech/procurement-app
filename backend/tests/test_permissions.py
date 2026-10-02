"""数据级权限的纯函数测试（M3）。

按角色算「可见范围」，是 M4 之后所有查询都要挂在 WHERE 上的那条条件。
这一层是纯 Python：不碰数据库、不碰 HTTP。

验收对应关系（PROGRESS.md M3）：
    REQUESTER 只看自己 / DEPT_MANAGER 看本部门 / PROCUREMENT 跨部门 / ADMIN 全部
"""

import pytest

from app.domain.permissions import Visibility, resolve_visibility

pytestmark = pytest.mark.unit


def test_requester_only_sees_own_requests() -> None:
    assert resolve_visibility(["REQUESTER"]) is Visibility.SELF


def test_department_manager_sees_own_department() -> None:
    assert resolve_visibility(["DEPT_MANAGER"]) is Visibility.DEPARTMENT


def test_department_head_sees_own_department() -> None:
    assert resolve_visibility(["DEPT_HEAD"]) is Visibility.DEPARTMENT


@pytest.mark.parametrize("role", ["PROCUREMENT", "FINANCE", "RECEIVER", "GM"])
def test_company_wide_roles_see_across_departments(role: str) -> None:
    """这四种角色的职责就是跨部门，看不见别的部门反而干不了活。"""
    assert resolve_visibility([role]) is Visibility.ALL


def test_admin_sees_everything() -> None:
    assert resolve_visibility(["ADMIN"]) is Visibility.ALL


def test_unknown_role_falls_back_to_the_narrowest_scope() -> None:
    """认不出的角色一律按最保守处理 —— 权限兜底方向只能是更小，不能更大。"""
    assert resolve_visibility(["SOME_FUTURE_ROLE"]) is Visibility.SELF


def test_no_roles_is_the_narrowest_scope() -> None:
    assert resolve_visibility([]) is Visibility.SELF


def test_widest_role_wins_when_a_user_holds_several() -> None:
    """ADMIN 兼任 GM、或主管兼任采购时，取更宽的那个，不能被窄的压回去。"""
    assert resolve_visibility(["REQUESTER", "PROCUREMENT"]) is Visibility.ALL
    assert resolve_visibility(["REQUESTER", "DEPT_MANAGER"]) is Visibility.DEPARTMENT


def test_visibility_orders_from_narrow_to_wide() -> None:
    """顺序本身是逻辑的一部分：取最大靠它，别把顺序改反。"""
    assert Visibility.SELF < Visibility.DEPARTMENT < Visibility.ALL

"""组织架构的纯函数（M5）：部门树展开。

这一层是纯 Python，**不 import 任何框架**，也不碰数据库 ——
调用方把已经查出来的部门列表传进来。

============================ 为什么可见范围要用到它 ============================
"部门主管看本部门"里的"本部门"**包含子部门**：后勤组挂在行政部下，
行政部主管当然要能看到后勤组提的申请。少了这一步，子部门的申请会没人看得到，
而**上级完全不会发现自己看不见** —— 权限范围算窄了不报错，只会少几行数据。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping

#: 递归展开时的层数上限。部门树正常不超过四五层；
#: 定这个上限是防"数据成环"（A 的上级是 B、B 的上级又是 A）时无限递归。
MAX_DEPTH = 16


def descendant_ids(
    root_id: uuid.UUID,
    parents: Mapping[uuid.UUID, uuid.UUID | None],
) -> set[uuid.UUID]:
    """展开 `root_id` 的全部子孙部门（不含自己）。

    `parents` 是"部门 id -> 上级 id"的映射，由调用方一次查出来。
    成环时靠 `MAX_DEPTH` 停住，不会把请求拖死。
    """
    children: dict[uuid.UUID, list[uuid.UUID]] = {}
    for dept_id, parent_id in parents.items():
        if parent_id is not None:
            children.setdefault(parent_id, []).append(dept_id)

    found: set[uuid.UUID] = set()
    frontier = [root_id]
    for _ in range(MAX_DEPTH):
        next_frontier: list[uuid.UUID] = []
        for node in frontier:
            for child in children.get(node, ()):
                if child in found or child == root_id:
                    continue  # child == root_id 说明成环，直接跳过
                found.add(child)
                next_frontier.append(child)
        if not next_frontier:
            break
        frontier = next_frontier
    return found


def parent_map(
    rows: Iterable[tuple[uuid.UUID, uuid.UUID | None]],
) -> dict[uuid.UUID, uuid.UUID | None]:
    """把 `(id, parent_id)` 序列变成映射。给 `descendant_ids` 用。"""
    return {dept_id: parent_id for dept_id, parent_id in rows}

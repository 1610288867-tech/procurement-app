"""主数据接口：部门 / 品类 / 供应商 / 预算。

边界层只做三件事：校验输入、装配依赖、把记录转成响应模型。
业务规则在 `domain/`，事务边界在 `application/`。

============================ 2026-10-03：供应商列表收口 ============================
原先 `/api/v1/suppliers` 是**完全公开**的，而且带一个**客户端可控**的 `only_active` 参数
（传 `false` 就能索取已停用的供应商）。这违反两条：

  - 内控指引第 7 条把**合格供应商清单**划给采购/管理职能（"授予相应的请购权"是前提）；
  - OWASP 的最小权限与"默认拒绝"：最窄的角色不该拿到"要不要过滤"的开关。

所以两处一起改：**该接口改为要求登录**，`only_active` **从客户端参数收掉**
（合格过滤写死；要看全部请走 `/api/v1/admin/suppliers`，那里本来就要求 ADMIN）。
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.coordinators.catalog import CatalogCoordinator
from app.boundaries.deps import CurrentUser, require_roles
from app.database import get_session
from app.domain.permissions import ROLE_ADMIN, ROLE_FINANCE
from app.infrastructure.repositories.budget_repo import BudgetRepository
from app.infrastructure.repositories.catalog_repo import CatalogRepository

router = APIRouter(tags=["主数据"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def catalog_coordinator(session: AsyncSession) -> CatalogCoordinator:
    """装配依赖。仓库与会话在结构上满足 application/ports.py 里的协议。"""
    return CatalogCoordinator(
        catalog=CatalogRepository(session),
        uow=session,
        ledger=BudgetRepository(session),
    )


# ---------------------------------------------------------------- 响应模型


class SupplierOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    code: str
    active: bool
    qualification_expires_at: datetime | None = None


class DepartmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    code: str
    parent_id: uuid.UUID | None = None
    active: bool


class CategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    code: str
    form_schema: dict
    approval_policy: dict
    version: int
    active: bool


class BudgetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    department_id: uuid.UUID
    category_id: uuid.UUID
    year: int
    # 金额一律整数「分」，字段名带 _fen 提醒调用方
    amount_total_fen: int = Field(validation_alias="amount_total")
    amount_reserved_fen: int = Field(validation_alias="amount_reserved")
    amount_spent_fen: int = Field(validation_alias="amount_spent")


# -------------------------------------------------------------------- 供应商


@router.get("/api/v1/suppliers", response_model=list[SupplierOut], summary="列出合格供应商")
async def list_suppliers(
    user: CurrentUser, session: SessionDep
) -> list[SupplierOut]:
    """只返回**合格**供应商：未停用、且资质未过期（业务规则第 4 条）。

    过期的按查询时过滤挡在外面，不用定时任务 —— 定时任务没跑成，
    过期供应商就会一直挂在可选列表里。

    **过滤条件是写死的，不接受客户端传参**（`only_active` 已于 2026-10-03 收掉）：
    否则一个最窄的角色只要加 `?only_active=false` 就能拿到已停用的供应商，
    而"合格供应商清单"本身是采购/管理职能的资产（内控指引第 7 条）。
    需要看全部（含停用）请走 `/api/v1/admin/suppliers`，那个接口要求 ADMIN。
    """
    records = await catalog_coordinator(session).list_suppliers(only_active=True)
    return [SupplierOut.model_validate(record) for record in records]


# -------------------------------------------------------------------- 部门


@router.get("/api/v1/departments", response_model=list[DepartmentOut], summary="列出部门")
async def list_departments(
    user: CurrentUser, session: SessionDep
) -> list[DepartmentOut]:
    """**可选的**部门列表（只返回启用中的），供表单下拉用。

    两处收口（2026-10-03），与供应商列表同形：

    1. **要求登录**：原先是公开接口。这两个接口只在**登录后的界面**里用得到
       （预算页的部门下拉、新建申请页的品类下拉），公开没有任何收益；
    2. **`only_active` 收掉**：它是客户端可控的，传 `false` 就能拿到已停用的部门。
       与供应商那次一样 —— 最窄的角色不该有"要不要过滤"的决定权（OWASP 最小权限）。

    要看全部（含停用）请走 `/api/v1/admin/*`（要求 ADMIN）。
    """
    records = await catalog_coordinator(session).list_departments(only_active=True)
    return [DepartmentOut.model_validate(record) for record in records]


# ---------------------------------------------------------------- 采购品类


@router.get("/api/v1/categories", response_model=list[CategoryOut], summary="列出采购品类")
async def list_categories(user: CurrentUser, session: SessionDep) -> list[CategoryOut]:
    """**可选的**品类列表（只返回启用中的），供表单下拉用。收口理由同 `list_departments`。"""
    records = await catalog_coordinator(session).list_categories(only_active=True)
    return [CategoryOut.model_validate(record) for record in records]


@router.get(
    "/api/v1/categories/{category_id}",
    response_model=CategoryOut,
    summary="查看采购品类",
    responses={404: {"description": "品类不存在"}},
)
async def get_category(
    category_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> CategoryOut:
    """单个品类。

    **要求登录**（2026-10-03）：它会把整份 `approval_policy` 带出来 ——
    金额阈值（1000 / 10000 / 100000 元）与各级审批角色。
    那是**内控结构信息**，匿名可得等于对外公开"多少钱需要谁批"。
    """
    record = await catalog_coordinator(session).get_category(category_id)
    return CategoryOut.model_validate(record)


# -------------------------------------------------------------------- 预算


@router.get(
    "/api/v1/budgets/mine",
    response_model=list[BudgetOut],
    summary="查看本部门预算",
)
async def list_my_budgets(
    user: CurrentUser,
    session: SessionDep,
    year: Annotated[int | None, Query(ge=2000, le=2100)] = None,
) -> list[BudgetOut]:
    """**当前账号所属部门**的预算。

    两处收口（2026-10-03），都是 M2 留下的：

    1. **要求登录**：原先是公开接口，未登录就能读到预算金额与部门/品类 id ——
       而预算是财务数据，不该匿名可读（内控指引第 7 号把预算审核列为付款前置）。
    2. **`department_id` 不再由调用方指定**：它必须从登录态推导。
       否则任何人传一个部门 id 就能看到别的部门有多少钱 ——
       这正是 M2 注释里写的"M3 起改为从登录态推导"，当时没做。

    没有部门的账号（如系统管理员）返回**空列表**：他本来就不属于任何部门。
    要看全部预算请走 `/api/v1/admin/budgets`（要求 ADMIN）。
    """
    if user.department_id is None:
        return []
    records = await catalog_coordinator(session).list_budgets(
        department_id=user.department_id, year=year
    )
    return [BudgetOut.model_validate(record) for record in records]


# ------------------------------------------------------- 预算看板（M7，只读）


class BudgetBoardOut(BudgetOut):
    """看板行 = 预算 + 部门名 + 品类名。

    名字由后端带出来，而不是让前端再查两个接口：财务没有"列出全部部门 / 品类"
    的权限（那是管理员接口），而且少两次往返就少两处可能 403 的地方。
    """

    department_name: str
    category_name: str


class BudgetTransactionOut(BaseModel):
    """一条预算流水。`amount_fen` 永远是正数，方向由 `kind` 表达。"""

    id: uuid.UUID
    budget_id: uuid.UUID
    request_id: uuid.UUID
    request_code: str
    kind: str
    amount_fen: int
    created_at: datetime


@router.get(
    "/api/v1/budgets",
    response_model=list[BudgetBoardOut],
    summary="预算看板（全部部门 / 品类）",
    dependencies=[Depends(require_roles(ROLE_FINANCE, ROLE_ADMIN))],
)
async def list_budgets_for_board(
    session: SessionDep,
    year: Annotated[int | None, Query(ge=2000, le=2100)] = None,
) -> list[BudgetBoardOut]:
    """**财务与管理员的预算看板**：部门 × 品类的总额 / 已预留 / 已花 / 可用。

    为什么单开一个接口，而不是把 `/admin/budgets` 的权限放宽：
    两个动作要分别收紧 —— **看钱**是财务的职责（内控指引第 7 号把"审核采购预算"
    列为付款前置），**改钱**只该留给管理员。混成一个接口就只能取两者的并集。

    可用额由前端按 `总额 − 已预留 − 已花` 算（公式的唯一落点在后端
    `domain/budget_policy.available_fen`，这里返回三个原始数字，不重复算一遍）。
    """
    rows = await catalog_coordinator(session).list_all_budgets(year=year)
    # 用 `model_validate` 而不是直接构造：金额字段带 `validation_alias`
    # （库里叫 `amount_total`，响应里叫 `amount_total_fen`），走别名一次就能对齐，
    # 与其它两个预算接口保持同一种写法。
    return [
        BudgetBoardOut.model_validate(
            {
                "id": record.id,
                "department_id": record.department_id,
                "category_id": record.category_id,
                "year": record.year,
                "amount_total": record.amount_total,
                "amount_reserved": record.amount_reserved,
                "amount_spent": record.amount_spent,
                "department_name": department_name,
                "category_name": category_name,
            }
        )
        for record, department_name, category_name in rows
    ]


@router.get(
    "/api/v1/budgets/transactions",
    response_model=list[BudgetTransactionOut],
    summary="预算流水（RESERVE / RELEASE / SPEND）",
    dependencies=[Depends(require_roles(ROLE_FINANCE, ROLE_ADMIN))],
)
async def list_budget_transactions(
    session: SessionDep,
    budget_id: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[BudgetTransactionOut]:
    """最近的预算流水，**新到旧**，带申请单号。

    这张表是"账目对不对得上"的唯一依据：预留、释放（含差额）、结转各一条。
    看板上把它列出来，M7 验收里"能看到三类流水"就不再只能靠 SQL 验证。
    """
    rows = await catalog_coordinator(session).list_budget_transactions(
        budget_id=budget_id, limit=limit
    )
    return [
        BudgetTransactionOut(
            id=record.id,
            budget_id=record.budget_id,
            request_id=record.request_id,
            request_code=code,
            kind=record.kind,
            amount_fen=record.amount,
            created_at=record.created_at,
        )
        for record, code in rows
    ]

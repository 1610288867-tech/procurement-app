"""管理员接口：主数据的写入与配置。

这里是**三层校验的第一层（结构）**：用 Pydantic 把形状卡死 ——
role 只能是那 5 个之一、order 只能是 0..1000、steps 不能为空。

第二层（语义：路径白名单 / 整数分 / 零阈值兜底 / order 不重复）
在 `application/coordinators/catalog.py` 里调用 `domain/rules.py`。
第三层（每个步骤可达）在 M5。

M3 起这个路由组整体要求 `ADMIN` 角色（见下面的 router 定义）。
注意这是"**能不能用这个接口**"；"能看到哪些行"是另一套，见 domain/permissions.py。
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.boundaries.deps import require_roles
from app.boundaries.http.catalog import (
    BudgetOut,
    CategoryOut,
    DepartmentOut,
    SupplierOut,
    catalog_coordinator,
)
from app.database import get_session
from app.domain.permissions import ROLE_ADMIN
from app.errors import BusinessError

# 整组要求管理员：主数据是"配置"，不是日常业务动作。
# 挂在 router 上而不是逐个路由上 —— 少写 9 次，也就少 9 次漏写的机会。
router = APIRouter(
    prefix="/api/v1/admin",
    tags=["管理员·主数据"],
    dependencies=[Depends(require_roles(ROLE_ADMIN))],
)

SessionDep = Annotated[AsyncSession, Depends(get_session)]

ApprovalRole = Literal["DEPT_MANAGER", "DEPT_HEAD", "PROCUREMENT", "FINANCE", "GM"]
ApprovalMode = Literal["SINGLE", "ALL"]


# ---------------------------------------------------- 审批策略（结构层）


class ApprovalStepIn(BaseModel):
    order: int = Field(ge=0, le=1000, description="审批顺序，决定审批链的先后")
    role: ApprovalRole
    mode: ApprovalMode = Field(default="SINGLE", description="SINGLE 或签 / ALL 会签")
    when: dict[str, Any] = Field(
        description="受限 DSL 条件；金额字段叫 amount_fen（单位：分），不是 amount"
    )


class ApprovalPolicyIn(BaseModel):
    steps: list[ApprovalStepIn] = Field(min_length=1)
    skip_when_framework_contract: bool = False
    urgency_extra_step: ApprovalStepIn | None = None


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    code: str = Field(min_length=1, max_length=64)
    form_schema: dict[str, Any] = Field(default_factory=dict)
    approval_policy: ApprovalPolicyIn


class ApprovalPolicyPatch(BaseModel):
    approval_policy: ApprovalPolicyIn


# ---------------------------------------------------------------- 其余入参


class SupplierIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    code: str = Field(min_length=1, max_length=64)
    qualification_expires_at: str | None = Field(
        default=None, description="资质有效期，ISO-8601（如 2027-01-01T00:00:00+08:00）"
    )


class SupplierDisableIn(BaseModel):
    reason: str = Field(min_length=1, max_length=200, description="停用原因（要留痕）")


class DepartmentIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    code: str = Field(min_length=1, max_length=64)
    parent_id: uuid.UUID | None = None


class BudgetIn(BaseModel):
    department_id: uuid.UUID
    category_id: uuid.UUID
    year: int = Field(ge=2000, le=2100)
    amount_fen: int = Field(ge=0, description="预算总额，单位：分（1000 元 = 100000）")


class OkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ok: bool = True


# -------------------------------------------------------------------- 供应商


@router.get(
    "/suppliers",
    response_model=list[SupplierOut],
    summary="列出供应商（含已停用、已过资质有效期）",
)
async def admin_list_suppliers(session: SessionDep) -> list[SupplierOut]:
    """管理员列表要看得全：停用的、资质过期的都得在这儿能查到。

    否则"这家供应商怎么不见了"就没法排查 —— 公开列表按规则把它们藏起来是对的，
    但管理端必须留有它们的痕迹（`disabled_reason` / `qualification_expires_at`）。
    """
    records = await catalog_coordinator(session).list_suppliers(
        only_active=False, only_qualified=False
    )
    return [SupplierOut.model_validate(record) for record in records]


@router.post(
    "/suppliers",
    response_model=SupplierOut,
    status_code=201,
    summary="新增供应商",
    responses={409: {"description": "供应商编码已存在"}},
)
async def admin_create_supplier(payload: SupplierIn, session: SessionDep) -> SupplierOut:
    expiry = _parse_optional_datetime(payload.qualification_expires_at)
    record = await catalog_coordinator(session).create_supplier(
        name=payload.name, code=payload.code, qualification_expires_at=expiry
    )
    return SupplierOut.model_validate(record)


@router.post(
    "/suppliers/{supplier_id}/disable",
    response_model=OkOut,
    summary="停用供应商",
    responses={404: {"description": "供应商不存在或已停用"}},
)
async def admin_disable_supplier(
    supplier_id: uuid.UUID, payload: SupplierDisableIn, session: SessionDep
) -> OkOut:
    await catalog_coordinator(session).disable_supplier(
        supplier_id=supplier_id, reason=payload.reason
    )
    return OkOut()


# -------------------------------------------------------------------- 部门


@router.post(
    "/departments",
    response_model=DepartmentOut,
    status_code=201,
    summary="新增部门",
    responses={409: {"description": "部门编码已存在"}, 422: {"description": "上级部门不存在"}},
)
async def admin_create_department(
    payload: DepartmentIn, session: SessionDep
) -> DepartmentOut:
    record = await catalog_coordinator(session).create_department(
        name=payload.name, code=payload.code, parent_id=payload.parent_id
    )
    return DepartmentOut.model_validate(record)


# ---------------------------------------------------------------- 采购品类


@router.get(
    "/categories",
    response_model=list[CategoryOut],
    summary="列出采购品类（含已停用）",
)
async def admin_list_categories(session: SessionDep) -> list[CategoryOut]:
    records = await catalog_coordinator(session).list_categories(only_active=False)
    return [CategoryOut.model_validate(record) for record in records]


@router.post(
    "/categories",
    response_model=CategoryOut,
    status_code=201,
    summary="新增采购品类",
    responses={
        409: {"description": "品类编码已存在"},
        422: {"description": "审批策略结构或语义不合法"},
    },
)
async def admin_create_category(payload: CategoryIn, session: SessionDep) -> CategoryOut:
    record = await catalog_coordinator(session).create_category(
        name=payload.name,
        code=payload.code,
        form_schema=payload.form_schema,
        # 第一层（Pydantic）已经过了，这里交给第二层（语义）继续查
        approval_policy=payload.approval_policy.model_dump(),
    )
    return CategoryOut.model_validate(record)


@router.put(
    "/categories/{category_id}/approval-policy",
    response_model=CategoryOut,
    summary="修改审批策略（版本号 +1）",
    responses={404: {"description": "品类不存在"}, 422: {"description": "策略不合法"}},
)
async def admin_update_approval_policy(
    category_id: uuid.UUID, payload: ApprovalPolicyPatch, session: SessionDep
) -> CategoryOut:
    record = await catalog_coordinator(session).update_approval_policy(
        category_id=category_id, approval_policy=payload.approval_policy.model_dump()
    )
    return CategoryOut.model_validate(record)


# -------------------------------------------------------------------- 预算


@router.get("/budgets", response_model=list[BudgetOut], summary="列出预算")
async def admin_list_budgets(
    session: SessionDep,
    department_id: uuid.UUID | None = None,
    year: int | None = None,
) -> list[BudgetOut]:
    records = await catalog_coordinator(session).list_budgets(
        department_id=department_id, year=year
    )
    return [BudgetOut.model_validate(record) for record in records]


@router.post(
    "/budgets",
    response_model=BudgetOut,
    status_code=201,
    summary="新增预算（部门 × 年度 × 品类）",
    responses={409: {"description": "该组合的预算已存在"}, 422: {"description": "部门或品类不存在"}},
)
async def admin_create_budget(payload: BudgetIn, session: SessionDep) -> BudgetOut:
    record = await catalog_coordinator(session).create_budget(
        department_id=payload.department_id,
        category_id=payload.category_id,
        year=payload.year,
        amount_fen=payload.amount_fen,
    )
    return BudgetOut.model_validate(record)


def _parse_optional_datetime(raw: str | None):
    """把 ISO-8601 文本转成 datetime；空值返回 None。

    非法格式直接 422 —— 边界层负责把"输入形状"卡住，
    不要让它带着错误的数据往下走。
    """
    if raw is None or raw == "":
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise BusinessError(
            f"qualification_expires_at 不是合法的 ISO-8601 时间：{raw}",
            status_code=422,
            code="INVALID_DATETIME",
        ) from exc

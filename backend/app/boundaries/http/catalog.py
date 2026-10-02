"""公开的主数据接口：部门 / 品类 / 供应商 / 预算。

边界层只做三件事：校验输入、装配依赖、把记录转成响应模型。
业务规则在 `domain/`，事务边界在 `application/`。

M3 会给这些接口加上按角色的权限依赖；M2 阶段全部公开。
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.coordinators.catalog import CatalogCoordinator
from app.database import get_session
from app.infrastructure.repositories.catalog_repo import CatalogRepository

router = APIRouter(tags=["主数据"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def catalog_coordinator(session: AsyncSession) -> CatalogCoordinator:
    """装配依赖。仓库与会话在结构上满足 application/ports.py 里的协议。"""
    return CatalogCoordinator(catalog=CatalogRepository(session), uow=session)


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


@router.get("/api/v1/suppliers", response_model=list[SupplierOut], summary="列出供应商")
async def list_suppliers(session: SessionDep, only_active: bool = True) -> list[SupplierOut]:
    """只返回**合格**供应商：未停用、且资质未过期（业务规则第 4 条）。

    过期的按查询时过滤挡在外面，不用定时任务 —— 定时任务没跑成，
    过期供应商就会一直挂在可选列表里。
    """
    records = await catalog_coordinator(session).list_suppliers(only_active=only_active)
    return [SupplierOut.model_validate(record) for record in records]


# -------------------------------------------------------------------- 部门


@router.get("/api/v1/departments", response_model=list[DepartmentOut], summary="列出部门")
async def list_departments(
    session: SessionDep, only_active: bool = True
) -> list[DepartmentOut]:
    records = await catalog_coordinator(session).list_departments(only_active=only_active)
    return [DepartmentOut.model_validate(record) for record in records]


# ---------------------------------------------------------------- 采购品类


@router.get("/api/v1/categories", response_model=list[CategoryOut], summary="列出采购品类")
async def list_categories(
    session: SessionDep, only_active: bool = True
) -> list[CategoryOut]:
    records = await catalog_coordinator(session).list_categories(only_active=only_active)
    return [CategoryOut.model_validate(record) for record in records]


@router.get(
    "/api/v1/categories/{category_id}",
    response_model=CategoryOut,
    summary="查看采购品类",
    responses={404: {"description": "品类不存在"}},
)
async def get_category(category_id: uuid.UUID, session: SessionDep) -> CategoryOut:
    record = await catalog_coordinator(session).get_category(category_id)
    return CategoryOut.model_validate(record)


# -------------------------------------------------------------------- 预算


@router.get("/api/v1/budgets/mine", response_model=list[BudgetOut], summary="查看预算")
async def list_my_budgets(
    session: SessionDep,
    department_id: Annotated[
        uuid.UUID | None, Query(description="M2 先由调用方指定；M3 起改为从登录态推导")
    ] = None,
    year: Annotated[int | None, Query(ge=2000, le=2100)] = None,
) -> list[BudgetOut]:
    records = await catalog_coordinator(session).list_budgets(
        department_id=department_id, year=year
    )
    return [BudgetOut.model_validate(record) for record in records]

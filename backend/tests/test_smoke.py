"""M0 冒烟测试：证明"测试链路能跑"。

验收标准里那条"`pytest` 能跑（哪怕只有一个空测试）"指的就是这个文件。

设计上刻意不加数据库依赖——它是 `unit` 档，永远秒回。
"""

import pytest

from app.config import get_settings
from app.errors import BusinessError, ConflictError, NotFoundError

pytestmark = pytest.mark.unit


def test_settings_load_with_defaults() -> None:
    """配置能加载，且默认连接串指向本地容器。"""
    settings = get_settings()
    assert settings.database_url.startswith("postgresql+asyncpg://")
    assert "@localhost:5432/procurement" in settings.database_url


def test_business_error_carries_status_and_code() -> None:
    """统一错误体依赖这三个属性，别改坏。"""
    err = BusinessError("预算不足", status_code=422, code="BUDGET_INSUFFICIENT")
    assert err.status_code == 422
    assert err.code == "BUDGET_INSUFFICIENT"
    assert err.message == "预算不足"


def test_conflict_is_409() -> None:
    """乐观锁/幂等冲突必须是 409，不是 400、不是 422。"""
    assert ConflictError().status_code == 409


def test_not_found_is_404() -> None:
    """不在可见范围内 → 404（不告诉他这东西存在）。"""
    assert NotFoundError().status_code == 404

"""统一错误类型与响应体。

**错误响应体的形状要在第一天定死**，因为前端已经在依赖它了：

    {"error": {"code": "...", "message": "...", "request_id": "..."}}

如果后端返回的还是 FastAPI 默认的 `{"detail": "..."}`，前端 `client.ts` 里
`body?.error?.message ?? 兜底文案` 的 `??` 后面那句会永远生效，
你就再也看不到真正的错误原因。
"""

from __future__ import annotations


class BusinessError(Exception):
    """业务规则不允许的操作。会被 main.py 的处理器转成统一错误体。

    用法：
        raise BusinessError("预算不足", status_code=422, code="BUDGET_INSUFFICIENT")
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 422,
        code: str = "BUSINESS_ERROR",
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class ConflictError(BusinessError):
    """版本过期或状态已变化 —— 对应 HTTP 409。

    乐观锁的失败、幂等键冲突、并发抢单失败，都用它。
    """

    def __init__(self, message: str = "状态或版本已变化，请刷新后重试") -> None:
        super().__init__(message, status_code=409, code="CONFLICT")


class NotFoundError(BusinessError):
    """资源不存在，**或者不在当前用户的可见范围内** —— 对应 HTTP 404。

    权限语义：资源在可见范围内但没权限操作 → 403；
    资源根本不在可见范围内 → 404（不告诉他这东西存在）。
    """

    def __init__(self, message: str = "资源不存在") -> None:
        super().__init__(message, status_code=404, code="NOT_FOUND")


class UnauthorizedError(BusinessError):
    """未登录、令牌无效/过期、凭证错误 —— 对应 HTTP 401。

    `code` 区分三种情形，前端靠它决定"跳登录页"还是"提示密码错"：
        UNAUTHORIZED        没登录 / 令牌失效
        INVALID_CREDENTIALS 用户名或口令不对
        TOO_MANY_ATTEMPTS   触发登录限流（429 也用这个 code，但状态码是 429）
    """

    def __init__(
        self, message: str = "未登录或登录已过期", *, code: str = "UNAUTHORIZED"
    ) -> None:
        super().__init__(message, status_code=401, code=code)


class TooManyAttemptsError(BusinessError):
    """登录失败太频繁 —— 对应 HTTP 429。

    为什么是 429 而不是 401：401 的意思是"你给错了凭证"，
    而这里的意思是"你给得太勤了，再试也没用"。前端据此可以禁用提交按钮。
    """

    def __init__(self, message: str = "登录失败次数过多，请稍后再试") -> None:
        super().__init__(message, status_code=429, code="TOO_MANY_ATTEMPTS")


class PermissionDeniedError(BusinessError):
    """可见但无权操作 —— 对应 HTTP 403。"""

    def __init__(self, message: str = "权限不足") -> None:
        super().__init__(message, status_code=403, code="FORBIDDEN")

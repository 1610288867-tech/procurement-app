"""附件接口（M9）：上传、列表、下载。

============================ 三条纪律，缺一条就出事 ============================
1. **磁盘上的名字永远由服务端生成**（`infrastructure/object_store.py`），
   用户文件名只当元数据 —— 否则 `../../app/main.py` 就能覆盖源码。
2. **`uploads/` 绝不挂成静态目录**。附件只能经本文件的下载路由出来，
   那条路径上有鉴权；挂成静态目录等于"知道文件名就能下载"。
3. **下载一律 `Content-Disposition: attachment`**，并且 `Content-Type` 用
   **入库时校验过的那个**，不用请求里带的 —— 永远不要把用户上传的东西
   当页面渲染（那是把内容类型白名单之外的风险重新引回来）。

中文文件名的处理见 `_content_disposition`：RFC 5987 的 `filename*=UTF-8''…`，
直接塞进 header 会抛编码异常（计划里第 6 条坑）。

============================ 关于 `application/octet-stream` ============================
`curl -F`、脚本客户端、`python-multipart`（对没带类型的部分）都会送这个值。
那不是"另一种类型"，而是"**没告诉我**"，所以 `domain/attachments.py` 会用
**扩展名**补全类型 —— 但补完之后 magic bytes 与真解码一步都不会少。
边界层不需要为此写任何分支：规则全在领域层，这里只把错误转成统一错误体。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Response, UploadFile
from pydantic import BaseModel

from app.application.ports import AttachmentView
from app.boundaries.deps import CurrentUser, SessionDep

# 附件挂在申请上，所以装配直接复用采购申请的工厂 —— 可见范围、幂等、预算、
# 审批链全都在里面，另建一个只会多一份会漂移的装配代码。
# （依赖方向是单向的：本模块 import requests，requests 不知道附件的存在。）
from app.boundaries.http.requests import purchase_coordinator
from app.errors import BusinessError

router = APIRouter(tags=["附件"])

#: 与领域层的 `MAX_ATTACHMENT_BYTES` 一致，这里只是再声明一次给 OpenAPI 看。
#: 真正的判定在 `domain/attachments.py` —— 别在边界层重复实现一遍规则。
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


class AttachmentOut(BaseModel):
    """附件的元数据。**不含 `storage_key`** —— 那是服务端的存储细节，
    暴露出去等于告诉调用方"磁盘上叫什么"，没有任何好处。"""

    id: uuid.UUID
    request_id: uuid.UUID
    kind: str
    original_filename: str
    content_type: str
    size_bytes: int
    sha256: str
    uploaded_by: uuid.UUID
    created_at: datetime

    @classmethod
    def of(cls, view: AttachmentView) -> AttachmentOut:
        return cls(
            id=view.id,
            request_id=view.request_id,
            kind=view.kind,
            original_filename=view.original_filename,
            content_type=view.content_type,
            size_bytes=view.size_bytes,
            sha256=view.sha256,
            uploaded_by=view.uploaded_by,
            created_at=view.created_at,
        )


def _content_disposition(filename: str) -> str:
    """把文件名安全地塞进 `Content-Disposition`。

    非 ASCII 文件名（中文是最常见的情况）**不能**直接写进 header：
    HTTP 头是 latin-1，Starlette 会在编码时抛 `UnicodeEncodeError`，接口直接 500。
    正确写法是 RFC 5987 的 `filename*=UTF-8''<百分号编码>`；
    同时给一个 ASCII 版 `filename=` 兜住不认识 `filename*` 的老客户端。
    """
    ascii_fallback = filename.encode("ascii", "replace").decode("ascii").replace('"', "_")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(filename)}"


@router.post(
    "/api/v1/requests/{request_id}/attachments",
    response_model=AttachmentOut,
    status_code=201,
    summary="上传附件（PDF / JPEG / PNG，≤ 10 MB）",
)
async def upload_attachment(
    request_id: uuid.UUID,
    user: CurrentUser,
    session: SessionDep,
    file: Annotated[UploadFile, File(description="要上传的文件")],
    kind: Annotated[str | None, Form(description="用途：QUOTATION/CONTRACT/INVOICE/RECEIPT/OTHER")] = None,
) -> AttachmentOut:
    """上传。

    `UploadFile` 已经把内容读进内存（10 MB 上限之内），这里 `await file.read()`
    拿到字节交给领域层的六步校验 —— **先校验、后落盘**，不合格的字节永远不落地。
    """
    content = await file.read()
    view = await purchase_coordinator(session).upload_attachment(
        request_id=request_id,
        actor_id=user.id,
        roles=user.roles,
        department_id=user.department_id,
        filename=file.filename,
        declared_type=file.content_type,
        content=content,
        kind=kind,
    )
    return AttachmentOut.of(view)


@router.get(
    "/api/v1/requests/{request_id}/attachments",
    response_model=list[AttachmentOut],
    summary="某张申请的附件列表（新的在前）",
)
async def list_attachments(
    request_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> list[AttachmentOut]:
    rows = await purchase_coordinator(session).list_attachments(
        request_id=request_id,
        actor_id=user.id,
        roles=user.roles,
        department_id=user.department_id,
    )
    return [AttachmentOut.of(row) for row in rows]


@router.get(
    "/api/v1/attachments/{attachment_id}/download",
    summary="下载附件（鉴权；一律作为附件下载，不内联渲染）",
    response_class=Response,
    responses={
        403: {"description": "附件存在，但它挂的申请不在你的可见范围内"},
        404: {"description": "不存在，或已被删除"},
    },
)
async def download_attachment(
    attachment_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> Response:
    view, content = await purchase_coordinator(session).open_attachment(
        attachment_id=attachment_id,
        actor_id=user.id,
        roles=user.roles,
        department_id=user.department_id,
    )
    if len(content) == 0:
        # 库里记着、磁盘上没有：这是**必须让人看见**的不一致，不能静默返回空文件
        raise BusinessError(
            "附件内容缺失（存储与记录不一致），请联系管理员核对",
            status_code=500,
            code="ATTACHMENT_CONTENT_MISSING",
        )
    return Response(
        content=content,
        media_type=view.content_type,
        headers={
            "Content-Disposition": _content_disposition(view.original_filename),
            # 内容不可变（同一 id 永远同一份字节），让浏览器放心缓存；
            # 但**不要** public：这是有权限的内容，中介缓存不该存。
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )

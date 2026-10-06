"""附件接口的集成测试（M9）：真数据库 + 真磁盘（临时目录）。

验收项对齐 `PROGRESS.md` 的 M9 清单：

    上传 PDF 成功，返回附件 id
    上传 11 MB → 422
    改名成 .pdf 的 exe → 422
    文件头是 PNG、内容不是图片 → 422（解码失败）
    超大尺寸 PNG（20000×20000）→ 422（压缩炸弹）
    越权下载附件 → 403
    直接访问 /uploads/<文件名> 拿不到文件
    下载响应带 Content-Disposition: attachment
    时间线按正序展示全部事件

============================ 两个容易忽略的地方 ============================
1. **上传目录必须指到临时目录**，不能写进 `backend/uploads/`：
   测试往仓库里丢文件是不可接受的（而且会随测试次数无限增长）。
   装配在**每次请求时**读 `settings`（`purchase_coordinator` 里 `get_settings()`），
   所以 `monkeypatch.setenv` 立刻生效 —— 见 `upload_dir` fixture。
2. **越权下载要 403，不是 404**：附件确实存在，只是它挂的申请不在你的可见范围内。
   两种语义的区别见 `boundaries/deps.py` 的文件头。
"""

from __future__ import annotations

import uuid
from io import BytesIO
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import delete

from app.database import SessionFactory
from app.domain.enums import ATTACH_QUOTATION, TX_RESERVE
from app.domain.permissions import ROLE_DEPT_MANAGER, ROLE_REQUESTER
from app.infrastructure.records.approval import ApprovalFlowRecord, ApprovalTaskRecord
from app.infrastructure.records.attachment import AttachmentRecord
from app.infrastructure.records.auth import UserRecord
from app.infrastructure.records.catalog import (
    BudgetRecord,
    BudgetTransactionRecord,
    DepartmentRecord,
    PurchaseCategoryRecord,
)
from app.infrastructure.records.purchase import (
    IdempotencyRecord,
    PurchaseItemRecord,
    PurchaseRequestRecord,
    RequestEventRecord,
)
from app.main import app
from tests.conftest import (
    Account,
    UserFactory,
    create_budget,
    login_as,
)

pytestmark = pytest.mark.integration

PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"

ONE_STEP_POLICY = {
    "steps": [
        {
            "order": 10,
            "role": "DEPT_MANAGER",
            "mode": "SINGLE",
            "when": {"gte": ["amount_fen", 0]},
        }
    ]
}


def _code(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


def make_png(size: tuple[int, int]) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, "white").save(buffer, "PNG")
    return buffer.getvalue()


@pytest.fixture
def upload_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把附件目录指到临时目录。

    `config.Settings` 用 `lru_cache`，但装配是在**每个请求**里调 `get_settings()` 的 ——
    环境变量改了之后新的 `Settings()` 会读到新值。这里同时清一次缓存，
    免得别的用例先调用过 `get_settings()` 把旧值钉住。
    """
    monkeypatch.setenv("UPLOAD_DIR", str(tmp_path))
    from app.config import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


class World:
    """一次用例的世界：两个部门（本部门 + 外人部门）+ 品类 + 预算 + 三个账号。"""

    def __init__(self) -> None:
        self.departments: list[uuid.UUID] = []
        self.categories: list[uuid.UUID] = []
        self.budgets: list[uuid.UUID] = []
        self.requests: list[uuid.UUID] = []
        self.actors: list[uuid.UUID] = []
        self.category: dict = {}
        self.requester: AsyncClient
        self.outsider: AsyncClient
        self._factory: UserFactory

    async def make(
        self, roles: list[str], department_id: uuid.UUID | None
    ) -> tuple[AsyncClient, Account]:
        account = await self._factory(roles=roles, department_id=department_id)
        self.actors.append(account.id)
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        await login_as(client, account)
        return client, account

    async def purge(self) -> None:
        """按外键顺序清理。附件与流水都是 CASCADE，但显式删一次更稳。"""
        async with SessionFactory() as session:
            if self.requests:
                await session.execute(
                    delete(AttachmentRecord).where(
                        AttachmentRecord.request_id.in_(self.requests)
                    )
                )
                await session.execute(
                    delete(ApprovalTaskRecord).where(
                        ApprovalTaskRecord.request_id.in_(self.requests)
                    )
                )
                await session.execute(
                    delete(ApprovalFlowRecord).where(
                        ApprovalFlowRecord.request_id.in_(self.requests)
                    )
                )
                await session.execute(
                    delete(BudgetTransactionRecord).where(
                        BudgetTransactionRecord.request_id.in_(self.requests)
                    )
                )
                await session.execute(
                    delete(RequestEventRecord).where(
                        RequestEventRecord.request_id.in_(self.requests)
                    )
                )
                await session.execute(
                    delete(PurchaseItemRecord).where(
                        PurchaseItemRecord.request_id.in_(self.requests)
                    )
                )
                await session.execute(
                    delete(PurchaseRequestRecord).where(
                        PurchaseRequestRecord.id.in_(self.requests)
                    )
                )
            if self.actors:
                await session.execute(
                    delete(IdempotencyRecord).where(
                        IdempotencyRecord.actor_id.in_(self.actors)
                    )
                )
                await session.execute(
                    delete(ApprovalTaskRecord).where(
                        ApprovalTaskRecord.decided_by.in_(self.actors)
                    )
                )
            if self.budgets:
                await session.execute(
                    delete(BudgetRecord).where(BudgetRecord.id.in_(self.budgets))
                )
            if self.categories:
                await session.execute(
                    delete(PurchaseCategoryRecord).where(
                        PurchaseCategoryRecord.id.in_(self.categories)
                    )
                )
            if self.departments:
                # 先删本部门里的测试账号，再删部门（users.department_id 是 RESTRICT）
                await session.execute(
                    delete(UserRecord).where(
                        UserRecord.username.like("t-%"),
                        UserRecord.department_id.in_(self.departments),
                    )
                )
                await session.execute(
                    delete(DepartmentRecord).where(
                        DepartmentRecord.id.in_(self.departments)
                    )
                )
            await session.commit()


@pytest.fixture
async def world(
    admin_client: AsyncClient, user_factory: UserFactory, upload_dir: Path
) -> World:
    """造一个自洽的世界；用例结束后按外键顺序清干净。

    `upload_dir` 挂在最前面：它把 `UPLOAD_DIR` 指向临时目录，
    这样本文件里所有上传都落在测试沙箱里，**不会脏了仓库的 `backend/uploads/`**。
    """
    state = World()
    state._factory = user_factory

    mine = (
        await admin_client.post(
            "/api/v1/admin/departments",
            json={"name": "附件测试部", "code": _code("TDEP")},
        )
    ).json()
    other = (
        await admin_client.post(
            "/api/v1/admin/departments",
            json={"name": "外人部门", "code": _code("TDEP")},
        )
    ).json()
    state.departments.extend([uuid.UUID(mine["id"]), uuid.UUID(other["id"])])

    category = (
        await admin_client.post(
            "/api/v1/admin/categories",
            json={
                "name": "附件测试品类",
                "code": _code("TCAT"),
                "approval_policy": ONE_STEP_POLICY,
            },
        )
    ).json()
    state.categories.append(uuid.UUID(category["id"]))
    state.category = category

    for department in (mine, other):
        budget = await create_budget(
            admin_client,
            department_id=department["id"],
            category_id=category["id"],
            amount_fen=100_000_000,
        )
        state.budgets.append(uuid.UUID(budget["id"]))

    state.requester, _ = await state.make([ROLE_REQUESTER], uuid.UUID(mine["id"]))
    # 本部门必须有一个 DEPT_MANAGER，否则提交时算不出审批人会 422
    # （`NO_APPROVER_FOR_STEP`）——这条链是"审批链不许为空"那条约束的体现。
    state.manager, _ = await state.make([ROLE_DEPT_MANAGER], uuid.UUID(mine["id"]))
    state.outsider, _ = await state.make([ROLE_REQUESTER], uuid.UUID(other["id"]))

    try:
        yield state
    finally:
        await state.requester.aclose()
        await state.manager.aclose()
        await state.outsider.aclose()
        await state.purge()


async def _draft(client: AsyncClient, world: World, amount_fen: int = 10_000) -> dict:
    """建一张**已提交**的申请（附件挂在提交后的单子上更贴近真实用法）。

    提交需要预算：world 已经给两个部门各配了一条。
    """
    created = await client.post(
        "/api/v1/requests", json={"category_id": world.category["id"], "title": "附件单"}
    )
    assert created.status_code == 201, created.text
    body = created.json()
    world.requests.append(uuid.UUID(body["id"]))

    items = await client.put(
        f"/api/v1/requests/{body['id']}/items",
        json={
            "version": body["version"],
            "items": [{"name": "测试物资", "quantity": 1, "unit_price_fen": amount_fen}],
        },
    )
    assert items.status_code == 200, items.text

    submitted = await client.post(
        f"/api/v1/requests/{body['id']}/submit",
        json={"version": items.json()["version"]},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert submitted.status_code == 200, submitted.text
    return submitted.json()


async def _upload(
    client: AsyncClient,
    request_id: str,
    *,
    filename: str,
    content: bytes,
    content_type: str,
    kind: str | None = None,
):
    data = {"kind": kind} if kind else None
    return await client.post(
        f"/api/v1/requests/{request_id}/attachments",
        files={"file": (filename, content, content_type)},
        data=data,
    )


# ================================================================ 1. 正常上传


async def test_upload_pdf_returns_id_and_is_listed(world: World, upload_dir: Path) -> None:
    """验收 1：上传 PDF 成功，返回附件 id；列表里也能看到它。"""
    request = await _draft(world.requester, world)
    response = await _upload(
        world.requester,
        request["id"],
        filename="报价单.pdf",
        content=PDF_BYTES,
        content_type="application/pdf",
        kind=ATTACH_QUOTATION,
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["request_id"] == request["id"]
    assert body["original_filename"] == "报价单.pdf"
    assert body["content_type"] == "application/pdf"
    assert body["size_bytes"] == len(PDF_BYTES)
    assert body["kind"] == ATTACH_QUOTATION
    assert len(body["sha256"]) == 64
    # storage_key 是服务端的存储细节，**不该**出现在响应里
    assert "storage_key" not in body

    listed = await world.requester.get(f"/api/v1/requests/{request['id']}/attachments")
    assert listed.status_code == 200, listed.text
    assert [row["id"] for row in listed.json()] == [body["id"]]


async def test_uploaded_bytes_land_under_a_uuid_name(world: World, upload_dir: Path) -> None:
    """**磁盘上的文件名是 UUID，不是用户给的名字。**

    这条直接查临时目录：如果哪天有人"顺手"用原始文件名存盘，
    这里会立刻红 —— 那正是路径穿越漏洞的起点。
    """
    request = await _draft(world.requester, world)
    # 文件名里塞穿越片段：它只该作为元数据存在，绝不该出现在路径里
    response = await _upload(
        world.requester,
        request["id"],
        filename="../../../evil.pdf",
        content=PDF_BYTES,
        content_type="application/pdf",
    )
    assert response.status_code == 201, response.text
    assert response.json()["original_filename"] == "../../../evil.pdf"

    files = [path for path in upload_dir.rglob("*") if path.is_file()]
    assert len(files) == 1, files
    stored = files[0]
    assert stored.name != "evil.pdf"
    assert stored.suffix == ".pdf"
    # 名字是 32 位十六进制（uuid4().hex）
    assert len(stored.stem) == 32 and all(ch in "0123456789abcdef" for ch in stored.stem)
    assert stored.read_bytes() == PDF_BYTES


async def test_unknown_kind_falls_back_to_other(world: World) -> None:
    """用途认不出 → `OTHER`，**不报错**（它只是给人分类的标签）。"""
    request = await _draft(world.requester, world)
    response = await _upload(
        world.requester,
        request["id"],
        filename="a.pdf",
        content=PDF_BYTES,
        content_type="application/pdf",
        kind="NOT_A_KIND",
    )
    assert response.status_code == 201, response.text
    assert response.json()["kind"] == "OTHER"


# ================================================================ 2. 六个拒绝面


async def test_upload_over_ten_mb_is_rejected(world: World, upload_dir: Path) -> None:
    """验收 2：11 MB → 422。**落盘目录必须仍然是空的**（先校验、后落盘）。"""
    request = await _draft(world.requester, world)
    oversized = PDF_BYTES + b"0" * (11 * 1024 * 1024)

    response = await _upload(
        world.requester,
        request["id"],
        filename="big.pdf",
        content=oversized,
        content_type="application/pdf",
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "FILE_TOO_LARGE"
    assert [p for p in upload_dir.rglob("*") if p.is_file()] == []


async def test_exe_renamed_to_pdf_is_rejected(world: World, upload_dir: Path) -> None:
    """验收 3：改名成 `.pdf` 的 exe → 422（文件头露馅）。"""
    request = await _draft(world.requester, world)
    exe = b"MZ\x90\x00\x03" + b"\x00" * 512

    response = await _upload(
        world.requester,
        request["id"],
        filename="发票.pdf",
        content=exe,
        content_type="application/pdf",
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "MAGIC_BYTES_MISMATCH"
    assert [p for p in upload_dir.rglob("*") if p.is_file()] == []


async def test_png_header_with_garbage_body_is_rejected(world: World) -> None:
    """验收 4：文件头是 PNG、内容不是图片 → 422（**解码失败**，不是头不符）。"""
    request = await _draft(world.requester, world)
    fake = b"\x89PNG\r\n\x1a\n" + b"this is definitely not a bitmap" * 4

    response = await _upload(
        world.requester,
        request["id"],
        filename="图.png",
        content=fake,
        content_type="image/png",
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "IMAGE_DECODE_FAILED"


async def test_oversized_png_is_rejected_as_compression_bomb(world: World) -> None:
    """验收 5：20000×20000 的 PNG → 422（压缩炸弹）。

    真造一张 4 亿像素的图会很慢也很吃内存，所以**改文件头**：
    PNG 的宽高就在 IHDR 里，把一份合法小图的 IHDR 尺寸改成 20000×20000，
    文件本身还是几百字节 —— 这恰好就是压缩炸弹的形状
    （文件小、解出来巨大）。CRC 会被破坏，所以先验的是"尺寸超限"而不是 CRC。
    """
    request = await _draft(world.requester, world)

    real = make_png((200, 200))
    # IHDR 的结构：8 字节签名 + 4 字节长度 + 4 字节 "IHDR" + 4 字节宽 + 4 字节高
    bomb = bytearray(real)
    bomb[16:20] = (20_000).to_bytes(4, "big")
    bomb[20:24] = (20_000).to_bytes(4, "big")

    response = await _upload(
        world.requester,
        request["id"],
        filename="bomb.png",
        content=bytes(bomb),
        content_type="image/png",
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] in {"IMAGE_TOO_LARGE", "IMAGE_DECODE_FAILED"}


async def test_type_outside_whitelist_is_rejected(world: World) -> None:
    """白名单之外一律 422（含 `image/svg+xml`：SVG 是能带脚本的文档，不是图片）。"""
    request = await _draft(world.requester, world)
    for declared in ("application/zip", "text/plain", "image/svg+xml"):
        response = await _upload(
            world.requester,
            request["id"],
            filename="x.bin",
            content=b"%PDF-1.7\n",  # 内容无所谓，类型先被拦下
            content_type=declared,
        )
        assert response.status_code == 422, (declared, response.text)
        assert response.json()["error"]["code"] == "TYPE_NOT_ALLOWED"


async def test_upload_to_a_request_out_of_scope_is_404(world: World) -> None:
    """可见范围之外的申请：上传 → **404**（不告诉他这张单子存在）。"""
    request = await _draft(world.requester, world)
    response = await _upload(
        world.outsider,
        request["id"],
        filename="a.pdf",
        content=PDF_BYTES,
        content_type="application/pdf",
    )
    assert response.status_code == 404, response.text


# ================================================================ 2.5 类型兜底（octet-stream）

async def test_octet_stream_pdf_is_accepted_via_extension_fallback(
    world: World, upload_dir: Path
) -> None:
    """**声明是 `application/octet-stream` 的真 PDF → 201**。

    这是给工具用的兜底：`curl -F`、脚本客户端、`python-multipart` 对没带类型的
    部分都会送这个值。挡掉它挡的是工具，不是攻击者 —— 攻击者会老实写
    `application/pdf`，那正是我们要的诚实声明。

    落库的类型必须是**补全出来的** `application/pdf`，不是 octet-stream。
    """
    request = await _draft(world.requester, world)
    response = await _upload(
        world.requester,
        request["id"],
        filename="报价单.pdf",
        content=PDF_BYTES,
        content_type="application/octet-stream",
    )
    assert response.status_code == 201, response.text
    assert response.json()["content_type"] == "application/pdf"

    # 事件快照里记下"类型是推断来的"，事后可追溯
    events = (
        await world.requester.get(f"/api/v1/requests/{request['id']}/timeline")
    ).json()["events"]
    added = [event for event in events if event["event_type"] == "ATTACHMENT_ADDED"]
    assert added[-1]["payload"]["type_inferred"] is True


async def test_exe_named_pdf_with_octet_stream_is_still_rejected(world: World) -> None:
    """**兜底的安全底线**：`evil.exe` 改名成 `evil.pdf` + 声明 octet-stream → 422。

    扩展名把类型补成 `application/pdf` 了，但文件头 `MZ` 立刻对不上 ——
    补全改变的只是"按哪种类型核对"，不是"核不核对"。
    """
    request = await _draft(world.requester, world)
    exe = b"MZ\x90\x00\x03" + b"\x00" * 512

    response = await _upload(
        world.requester,
        request["id"],
        filename="发票.pdf",
        content=exe,
        content_type="application/octet-stream",
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "MAGIC_BYTES_MISMATCH"


async def test_octet_stream_png_with_fake_body_is_still_rejected(world: World) -> None:
    """补成 `image/png` 之后，第 5 步的真解码也不能省。"""
    request = await _draft(world.requester, world)
    response = await _upload(
        world.requester,
        request["id"],
        filename="图.png",
        content=b"\x89PNG\r\n\x1a\n" + b"not a bitmap" * 8,
        content_type="application/octet-stream",
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "IMAGE_DECODE_FAILED"


async def test_octet_stream_with_unknown_extension_is_rejected(world: World) -> None:
    """补不出来 → 422（`.bin` 不在扩展名表里，**不猜**）。"""
    request = await _draft(world.requester, world)
    response = await _upload(
        world.requester,
        request["id"],
        filename="secret.bin",
        content=PDF_BYTES,  # 内容其实是个 PDF，但扩展名无法证明这一点
        content_type="application/octet-stream",
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "MISSING_CONTENT_TYPE"


# ================================================================ 3. 下载与越权


async def test_download_returns_the_bytes_with_attachment_disposition(world: World) -> None:
    """验收 8：下载响应带 `Content-Disposition: attachment`，中文名走 RFC 5987。"""
    request = await _draft(world.requester, world)
    uploaded = await _upload(
        world.requester,
        request["id"],
        filename="报价单 2026.pdf",
        content=PDF_BYTES,
        content_type="application/pdf",
    )
    attachment_id = uploaded.json()["id"]

    response = await world.requester.get(f"/api/v1/attachments/{attachment_id}/download")
    assert response.status_code == 200, response.text
    assert response.content == PDF_BYTES
    # 用**入库时校验过的**类型，不是请求里带的
    assert response.headers["content-type"] == "application/pdf"

    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert "filename*=UTF-8''" in disposition
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_download_of_someone_elses_attachment_is_403(world: World) -> None:
    """验收 6：**越权下载 → 403**（附件存在，但它挂的申请看不到）。

    与"可见范围外的申请 → 404"的区别，正是 `boundaries/deps.py` 那条越权语义：
    403 表示"这东西存在，但你没权限"，404 表示"我不告诉你它存不存在"。
    """
    request = await _draft(world.requester, world)
    uploaded = await _upload(
        world.requester,
        request["id"],
        filename="内部报价.pdf",
        content=PDF_BYTES,
        content_type="application/pdf",
    )
    attachment_id = uploaded.json()["id"]

    denied = await world.outsider.get(f"/api/v1/attachments/{attachment_id}/download")
    assert denied.status_code == 403, denied.text
    assert denied.json()["error"]["code"] == "FORBIDDEN"
    # 本人和**同部门**的人（这里用自己验证）仍然拿得到
    assert (
        await world.requester.get(f"/api/v1/attachments/{attachment_id}/download")
    ).status_code == 200


async def test_download_of_unknown_attachment_is_404(world: World) -> None:
    response = await world.requester.get(f"/api/v1/attachments/{uuid.uuid4()}/download")
    assert response.status_code == 404, response.text


async def test_soft_deleted_attachment_disappears_from_list_and_download(
    world: World,
) -> None:
    """软删除：列表里没了、下载变 404，但**行还在库里**（审计痕迹）。

    这条同时钉住"物理删除不提供"这条纪律：附件是审计证据。
    """
    request = await _draft(world.requester, world)
    uploaded = await _upload(
        world.requester,
        request["id"],
        filename="要删的.pdf",
        content=PDF_BYTES,
        content_type="application/pdf",
    )
    attachment_id = uuid.UUID(uploaded.json()["id"])

    from app.infrastructure.repositories.attachment_repo import AttachmentRepository

    async with SessionFactory() as session:
        repository = AttachmentRepository(session)
        assert await repository.soft_delete(attachment_id) is True
        # 第二次必须返回 False（条件更新的判定：deleted_at IS NULL 不成立）
        assert await repository.soft_delete(attachment_id) is False
        await session.commit()

    listed = await world.requester.get(f"/api/v1/requests/{request['id']}/attachments")
    assert listed.json() == []
    gone = await world.requester.get(f"/api/v1/attachments/{attachment_id}/download")
    assert gone.status_code == 404

    async with SessionFactory() as session:
        row = await session.get(AttachmentRecord, attachment_id)
        assert row is not None and row.deleted_at is not None


# ================================================================ 3.5 删除附件（接口层）


async def test_delete_endpoint_removes_it_from_list_and_keeps_the_file(
    world: World, upload_dir: Path
) -> None:
    """`DELETE /api/v1/attachments/{id}` → **204**；列表里没了、下载 404。

    关键的一条：**磁盘上的文件必须还在**（软删除）。
    物理删掉的话"当时附了什么"就永远查不出来 —— 这条断言就是那条纪律的守门人。
    """
    request = await _draft(world.requester, world)
    uploaded = await _upload(
        world.requester,
        request["id"],
        filename="要删的报价单.pdf",
        content=PDF_BYTES,
        content_type="application/pdf",
    )
    attachment_id = uploaded.json()["id"]
    assert len([path for path in upload_dir.rglob("*") if path.is_file()]) == 1

    deleted = await world.requester.delete(f"/api/v1/attachments/{attachment_id}")
    assert deleted.status_code == 204, deleted.text
    assert deleted.content == b""

    listed = await world.requester.get(f"/api/v1/requests/{request['id']}/attachments")
    assert listed.status_code == 200
    assert listed.json() == []
    assert (
        await world.requester.get(f"/api/v1/attachments/{attachment_id}/download")
    ).status_code == 404

    # 文件还在（软删除），行也还在（只是 deleted_at 非空）
    assert len([path for path in upload_dir.rglob("*") if path.is_file()]) == 1
    async with SessionFactory() as session:
        row = await session.get(AttachmentRecord, uuid.UUID(attachment_id))
        assert row is not None and row.deleted_at is not None


async def test_delete_is_idempotent_and_reports_404_on_second_call(world: World) -> None:
    """再删一次 → **404**（不是 409、更不是 500）。

    "你要删的东西已经没了"与"它从来不存在"对调用方是同一件事，
    而且这样重试天然幂等。
    """
    request = await _draft(world.requester, world)
    uploaded = await _upload(
        world.requester,
        request["id"],
        filename="删两次.pdf",
        content=PDF_BYTES,
        content_type="application/pdf",
    )
    attachment_id = uploaded.json()["id"]

    assert (
        await world.requester.delete(f"/api/v1/attachments/{attachment_id}")
    ).status_code == 204
    second = await world.requester.delete(f"/api/v1/attachments/{attachment_id}")
    assert second.status_code == 404, second.text


async def test_delete_by_an_outsider_is_403_and_the_attachment_survives(
    world: World,
) -> None:
    """外部门账号删 → **403**，而且附件**必须还在**（不能"报了错却删掉了"）。"""
    request = await _draft(world.requester, world)
    uploaded = await _upload(
        world.requester,
        request["id"],
        filename="别人删不掉.pdf",
        content=PDF_BYTES,
        content_type="application/pdf",
    )
    attachment_id = uploaded.json()["id"]

    denied = await world.outsider.delete(f"/api/v1/attachments/{attachment_id}")
    assert denied.status_code == 403, denied.text
    assert denied.json()["error"]["code"] == "FORBIDDEN"

    still_there = await world.requester.get(
        f"/api/v1/attachments/{attachment_id}/download"
    )
    assert still_there.status_code == 200
    assert still_there.content == PDF_BYTES


async def test_delete_writes_a_timeline_event(world: World) -> None:
    """删除要留痕：时间线多一条 `ATTACHMENT_REMOVED`，且**不改变状态**。

    没有这条事件的话，"这份合同怎么没了"事后永远查不出来 ——
    行被标记了、文件还在，但时间线上一片安静。
    """
    request = await _draft(world.requester, world)
    uploaded = await _upload(
        world.requester,
        request["id"],
        filename="留痕.pdf",
        content=PDF_BYTES,
        content_type="application/pdf",
    )
    attachment_id = uploaded.json()["id"]

    before = (
        await world.requester.get(f"/api/v1/requests/{request['id']}/timeline")
    ).json()
    assert (
        await world.requester.delete(f"/api/v1/attachments/{attachment_id}")
    ).status_code == 204
    after = (
        await world.requester.get(f"/api/v1/requests/{request['id']}/timeline")
    ).json()

    events = after["events"]
    assert len(events) == len(before["events"]) + 1
    last = events[-1]
    assert last["event_type"] == "ATTACHMENT_REMOVED"
    assert last["from_status"] == last["to_status"], "删除事件不该伪装成状态迁移"
    assert last["payload"]["attachment_id"] == attachment_id
    assert last["payload"]["filename"] == "留痕.pdf"
    # 上传那条事件仍在：删的是附件，不是历史
    assert any(
        event["event_type"] == "ATTACHMENT_ADDED" for event in events
    ), "上传那条事件不能被删掉"


async def test_delete_of_unknown_attachment_is_404(world: World) -> None:
    response = await world.requester.delete(f"/api/v1/attachments/{uuid.uuid4()}")
    assert response.status_code == 404, response.text


async def test_delete_without_login_is_401(world: World) -> None:
    request = await _draft(world.requester, world)
    uploaded = await _upload(
        world.requester,
        request["id"],
        filename="a.pdf",
        content=PDF_BYTES,
        content_type="application/pdf",
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        response = await anon.delete(f"/api/v1/attachments/{uploaded.json()['id']}")
    assert response.status_code == 401, response.text


async def test_uploads_directory_is_not_served_statically(world: World, upload_dir: Path) -> None:
    """验收 7：直接访问 `/uploads/<文件名>` **拿不到文件**。

    两层含义都要验：
      1. `/uploads/...` 这条路径在应用里根本不存在（没 mount 静态目录）；
      2. 就算知道了磁盘上的真实文件名，也一样拿不到 —— 附件只能经鉴权路由出来。
    """
    request = await _draft(world.requester, world)
    uploaded = await _upload(
        world.requester,
        request["id"],
        filename="secret.pdf",
        content=PDF_BYTES,
        content_type="application/pdf",
    )
    assert uploaded.status_code == 201, uploaded.text

    stored = next(path for path in upload_dir.rglob("*") if path.is_file())
    for path in ("/uploads/", f"/uploads/{stored.name}", f"/uploads/{stored.parent.name}/{stored.name}"):
        response = await world.requester.get(path)
        assert response.status_code == 404, (path, response.status_code)


# ================================================================ 4. 时间线


async def test_attachment_shows_up_in_the_timeline_without_changing_status(
    world: World,
) -> None:
    """验收 9 的一部分：附件事件进时间线，且**不改变状态**。

    验证方式与 M6 的 `APPROVAL_STARTED` 完全一致：看 `from_status == to_status`。
    如果哪天有人把"加附件"写成状态迁移，这条会红。
    """
    request = await _draft(world.requester, world)
    before = (await world.requester.get(f"/api/v1/requests/{request['id']}/timeline")).json()

    uploaded = await _upload(
        world.requester,
        request["id"],
        filename="报价单.pdf",
        content=PDF_BYTES,
        content_type="application/pdf",
    )
    assert uploaded.status_code == 201, uploaded.text

    after = (await world.requester.get(f"/api/v1/requests/{request['id']}/timeline")).json()
    events = after["events"]
    assert len(events) == len(before["events"]) + 1

    last = events[-1]
    assert last["event_type"] == "ATTACHMENT_ADDED"
    assert last["from_status"] == last["to_status"], "附件事件不该伪装成状态迁移"
    assert last["payload"]["filename"] == "报价单.pdf"
    assert last["payload"]["size_bytes"] == len(PDF_BYTES)
    assert last["payload"]["attachment_id"] == uploaded.json()["id"]

    # 时间线是**正序**：新事件必须在最后（排序靠 seq，不靠时间戳）
    assert [event["event_type"] for event in events][-1] == "ATTACHMENT_ADDED"


async def test_timeline_order_is_stable_with_many_attachments(world: World) -> None:
    """连续传 5 个附件：时间线里 5 条 `ATTACHMENT_ADDED` 的顺序必须是**上传顺序**。

    这正是 M8 修的排序退化最容易复发的场景（同一事务/同一微秒写入多条日志）——
    `request_events` 现在按 `seq` 排序，所以这里能稳定断言。
    """
    request = await _draft(world.requester, world)
    names = [f"附件-{index}.pdf" for index in range(5)]
    for name in names:
        response = await _upload(
            world.requester,
            request["id"],
            filename=name,
            content=PDF_BYTES,
            content_type="application/pdf",
        )
        assert response.status_code == 201, response.text

    events = (
        await world.requester.get(f"/api/v1/requests/{request['id']}/timeline")
    ).json()["events"]
    added = [
        event["payload"]["filename"]
        for event in events
        if event["event_type"] == "ATTACHMENT_ADDED"
    ]
    assert added == names


async def test_list_returns_newest_first(world: World) -> None:
    """附件列表：**新的在前**（与流水列表同一个约定）。"""
    request = await _draft(world.requester, world)
    for name in ("第一个.pdf", "第二个.pdf", "第三个.pdf"):
        response = await _upload(
            world.requester,
            request["id"],
            filename=name,
            content=PDF_BYTES,
            content_type="application/pdf",
        )
        assert response.status_code == 201, response.text

    listed = await world.requester.get(f"/api/v1/requests/{request['id']}/attachments")
    assert [row["original_filename"] for row in listed.json()] == [
        "第三个.pdf",
        "第二个.pdf",
        "第一个.pdf",
    ]


async def test_upload_does_not_touch_the_budget(world: World) -> None:
    """接缝检查：加附件**不该**动钱。

    它顺手证明"附件这条路径没有误闯预算逻辑"——申请只有一条 RESERVE 流水，
    传完附件还是只有一条（`UNIQUE(request_id, kind)` 会让重复记账直接报错）。
    """
    request = await _draft(world.requester, world)
    request_id = uuid.UUID(request["id"])

    async def ledger_kinds() -> list[str]:
        from sqlalchemy import select

        async with SessionFactory() as session:
            rows = await session.scalars(
                select(BudgetTransactionRecord.kind)
                .where(BudgetTransactionRecord.request_id == request_id)
                .order_by(BudgetTransactionRecord.seq)
            )
            return list(rows.all())

    before = await ledger_kinds()
    assert before == [TX_RESERVE]

    for index in range(3):
        response = await _upload(
            world.requester,
            request["id"],
            filename=f"n{index}.pdf",
            content=PDF_BYTES,
            content_type="application/pdf",
        )
        assert response.status_code == 201, response.text

    assert await ledger_kinds() == before


async def test_upload_without_login_is_401(world: World) -> None:
    """未登录上传 → 401（不是 403、更不是 201）。"""
    request = await _draft(world.requester, world)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anon:
        response = await anon.post(
            f"/api/v1/requests/{request['id']}/attachments",
            files={"file": ("a.pdf", PDF_BYTES, "application/pdf")},
        )
    assert response.status_code == 401, response.text

"""附件上传校验的**纯函数**测试（M9）。

不连数据库、不碰磁盘、不 import Pillow —— 六步校验里的第 2–6 步全部在这里跑完，
毫秒级。解码器用假的：真解码器在 `infrastructure/image_probe.py`，
它的行为由另一组「真实文件」用例覆盖（`tests/test_image_probe.py`）。

============================ 每条用例对应一种"得手方式" ============================
这份文件的组织方式刻意对齐 `domain/attachments.py` 的模块说明：
每一条不是"测一个函数"，而是"堵一种绕过手法"。
"""

from __future__ import annotations

import hashlib

import pytest

from app.domain.attachments import (
    GENERIC_CONTENT_TYPES,
    MAX_ATTACHMENT_BYTES,
    MAX_IMAGE_PIXELS,
    AttachmentError,
    infer_type_from_filename,
    normalize_content_type,
    resolve_content_type,
    safe_kind,
    validate_upload,
)

pytestmark = pytest.mark.unit

PNG_HEADER = b"\x89PNG\r\n\x1a\n"
JPEG_HEADER = b"\xff\xd8\xff"
PDF_HEADER = b"%PDF-1.7\n"

#: 替身解码器认它当"结构完整"。真解码器的判据见 `infrastructure/image_probe.py`。
PNG_TAIL = b"\x00\x00\x00\x00IEND\xaeB`\x82"


def fake_png(size: tuple[int, int] = (800, 600)):
    """假解码器：认 PNG 头**并且**要求出现 `IEND` 收尾块，才认它是一张图。

    ============================ 它为什么不能只认文件头 ============================
    第一版写成 `if content.startswith(PNG_HEADER): return size`，
    于是"PNG 头 + 一句废话"被判成合法图片 —— **测出来的绿色是假的**。
    真解码器（`infrastructure/image_probe.py`）会拒掉它：本机实测
    `PNG 头 + 随机字节` → `UnidentifiedImageError`、`PNG 截断一半` → `Truncated File Read`。

    所以这个替身必须**至少**比 magic bytes 严一档，否则它测不出第 5 步存在的意义
    （第 5 步的全部价值就是"文件头对、内容假也拦得住"）。
    """

    def decode(content: bytes) -> tuple[int, int]:
        if not content.startswith(PNG_HEADER) or b"IEND" not in content:
            raise ValueError("not a real png")
        return size

    return decode


# ---------------------------------------------------------------- 内容类型归一化


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("application/pdf", "application/pdf"),
        ("APPLICATION/PDF", "application/pdf"),
        ("application/pdf; charset=binary", "application/pdf"),
        ("  image/png  ", "image/png"),
        ("image/jpg", "image/jpeg"),  # 浏览器常送的非标准别名
        ("image/pjpeg", "image/jpeg"),
        ("", ""),
        (None, ""),
    ],
)
def test_content_type_is_normalized(raw, expected) -> None:
    """带参数、大小写、别名都要归一化。

    不归一化的后果不是"报错"，而是**误伤**：白名单匹配不上，
    开发者顺手把判定改成 `startswith("image/")`，白名单就废了。
    """
    assert normalize_content_type(raw) == expected


# ---------------------------------------------------------------- 第 2 步：大小


def test_empty_file_is_rejected() -> None:
    with pytest.raises(AttachmentError) as exc:
        validate_upload(filename="a.pdf", declared_type="application/pdf", content=b"")
    assert exc.value.code == "EMPTY_FILE"


def test_file_over_ten_mb_is_rejected() -> None:
    """11 MB → 422。上限本身是**断言**：改上限就必须改这条测试。"""
    oversized = PDF_HEADER + b"0" * (11 * 1024 * 1024)
    assert len(oversized) > MAX_ATTACHMENT_BYTES
    with pytest.raises(AttachmentError) as exc:
        validate_upload(
            filename="big.pdf", declared_type="application/pdf", content=oversized
        )
    assert exc.value.code == "FILE_TOO_LARGE"


def test_exactly_at_the_limit_is_accepted() -> None:
    """边界取"闭区间"：正好 10 MB 要**放行**（不然上限就不是 10 MB）。"""
    payload = PDF_HEADER + b"0" * (MAX_ATTACHMENT_BYTES - len(PDF_HEADER))
    assert len(payload) == MAX_ATTACHMENT_BYTES
    result = validate_upload(
        filename="edge.pdf", declared_type="application/pdf", content=payload
    )
    assert result.size_bytes == MAX_ATTACHMENT_BYTES


# ---------------------------------------------------------------- 第 3 步：白名单


@pytest.mark.parametrize(
    "declared",
    ["application/zip", "text/plain", "image/gif", "image/svg+xml"],
)
def test_types_outside_the_whitelist_are_rejected(declared: str) -> None:
    """**声明了**一个白名单之外的具体类型 → 422。

    `image/svg+xml` 也拒：SVG 是**可执行文档**（能带脚本），不是图片。
    这条容易被当成"图片当然要放行"，但它恰恰是上传接口里最经典的一个坑。

    注意这里**不用** generic 类型（`application/octet-stream`）做参数：
    那是"没告诉我"，会走扩展名兜底 —— 见下一节。
    """
    with pytest.raises(AttachmentError) as exc:
        validate_upload(filename="x.bin", declared_type=declared, content=b"whatever")
    assert exc.value.code == "TYPE_NOT_ALLOWED"


# ---------------------------------------------------------------- 第 3 步：类型兜底


def test_missing_content_type_is_rejected_when_extension_is_unknown() -> None:
    """既没声明类型、扩展名也认不出来 → 422（**不猜**）。"""
    with pytest.raises(AttachmentError) as exc:
        validate_upload(filename="a.bin", declared_type=None, content=PDF_HEADER)
    assert exc.value.code == "MISSING_CONTENT_TYPE"


@pytest.mark.parametrize("generic", sorted(GENERIC_CONTENT_TYPES))
def test_octet_stream_falls_back_to_the_extension(generic: str) -> None:
    """`application/octet-stream`（= "没告诉我"）用扩展名补全类型。

    这条兜底是给 `curl -F`、脚本客户端和 `python-multipart` 用的：
    它们对 PDF 也送 `application/octet-stream`，把它当"声明了别的类型"拒掉，
    挡掉的是工具而不是攻击者。
    """
    result = validate_upload(
        filename="报价单.pdf", declared_type=generic, content=PDF_HEADER + b"x"
    )
    assert result.content_type == "application/pdf"
    assert result.suffix == ".pdf"
    assert result.type_inferred is True


@pytest.mark.parametrize(
    ("filename", "expected_type", "expected_suffix"),
    [
        ("a.PDF", "application/pdf", ".pdf"),
        ("a.JpEg", "image/jpeg", ".jpg"),
        ("图.PNG", "image/png", ".png"),
    ],
)
def test_extension_fallback_is_case_insensitive(
    filename: str, expected_type: str, expected_suffix: str
) -> None:
    """Windows 上用户文件名大小写很随意，扩展名比对必须忽略大小写。"""
    content = PNG_HEADER + PNG_TAIL if expected_suffix == ".png" else JPEG_HEADER + b"\x00" * 8
    if expected_suffix == ".pdf":
        content = PDF_HEADER + b"x"
    result = validate_upload(
        filename=filename,
        declared_type="application/octet-stream",
        content=content,
        decode_image=lambda _content: (10, 10),
    )
    assert result.content_type == expected_type
    assert result.suffix == expected_suffix


def test_declared_type_wins_over_the_extension() -> None:
    """声明了**具体**类型时以它为准，扩展名不参与 —— 不符会被第 4 步抓出来。

    这条正是"兜底没有被用成绕过"的证据：`evil.pdf` 里装 PNG，声明成
    `application/pdf` 时是**按 pdf 核对**的，所以直接 MAGIC_BYTES_MISMATCH。
    （如果实现改成"扩展名优先"，这条会变成 201，一测就红。）
    """
    with pytest.raises(AttachmentError) as exc:
        validate_upload(
            filename="evil.pdf",
            declared_type="application/pdf",  # 具体声明，不是 generic
            content=PNG_HEADER + b"x",
        )
    assert exc.value.code == "MAGIC_BYTES_MISMATCH"


def test_extension_fallback_still_checks_magic_bytes() -> None:
    """**这条是本次兜底的安全底线**：扩展名补出类型之后，文件头照样要核。

    `evil.exe` 改名成 `evil.pdf` + 声明 `application/octet-stream`：
    类型补成 `application/pdf` 了，但 PE 头 `MZ` 立刻对不上 → 422。
    """
    exe = b"MZ\x90\x00\x03" + b"\x00" * 256
    with pytest.raises(AttachmentError) as exc:
        validate_upload(
            filename="发票.pdf",
            declared_type="application/octet-stream",  # 声明是"没告诉我"
            content=exe,  # 内容其实是 exe
        )
    assert exc.value.code == "MAGIC_BYTES_MISMATCH"


def test_extension_fallback_still_decodes_images() -> None:
    """扩展名补成 `image/png` 之后，第 5 步的解码一步都不会少。"""
    with pytest.raises(AttachmentError) as exc:
        validate_upload(
            filename="图.png",
            declared_type="application/octet-stream",
            content=PNG_HEADER + b"not a real png",
            decode_image=fake_png(),
        )
    assert exc.value.code == "IMAGE_DECODE_FAILED"


def test_extension_fallback_still_respects_the_whitelist() -> None:
    """补出来的类型也要在白名单里：`.exe` / `.gif` / `.svg` 全部 422。"""
    for filename in ("a.exe", "a.gif", "a.svg", "a.zip"):
        with pytest.raises(AttachmentError) as exc:
            validate_upload(
                filename=filename, declared_type="application/octet-stream", content=b"x" * 32
            )
        assert exc.value.code in {"MISSING_CONTENT_TYPE", "TYPE_NOT_ALLOWED"}, filename


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("a.pdf", "application/pdf"),
        ("a.jpeg", "image/jpeg"),
        ("a.jpg", "image/jpeg"),
        ("a.png", "image/png"),
        ("a.PDF", "application/pdf"),
        ("a.exe", ""),
        ("noextension", ""),
        (".pdf", ""),  # 只有扩展名、没有主名 → 不算有效文件名
        ("", ""),
        (None, ""),
        ("a.tar.gz", ""),  # 只看最后一段
    ],
)
def test_infer_type_from_filename(filename, expected) -> None:
    assert infer_type_from_filename(filename) == expected


def test_resolve_content_type_reports_whether_it_inferred() -> None:
    """`resolve_content_type` 要如实报告类型是不是推断来的（可追溯）。"""
    assert resolve_content_type("application/pdf", "a.png") == ("application/pdf", False)
    assert resolve_content_type("application/octet-stream", "a.png") == ("image/png", True)
    assert resolve_content_type(None, "a.pdf") == ("application/pdf", True)
    assert resolve_content_type("", "a.exe") == ("", True)


# ---------------------------------------------------------------- 第 4 步：magic bytes


def test_exe_renamed_to_pdf_is_rejected() -> None:
    """改名成 `.pdf` 的 exe：文件名与 Content-Type 都是假的，文件头是真的。"""
    exe = b"MZ\x90\x00\x03" + b"\x00" * 100  # Windows PE 头
    with pytest.raises(AttachmentError) as exc:
        validate_upload(filename="发票.pdf", declared_type="application/pdf", content=exe)
    assert exc.value.code == "MAGIC_BYTES_MISMATCH"


def test_png_bytes_declared_as_pdf_is_rejected() -> None:
    """反过来也要拒：内容与声明不符就是不符，没有"以内容为准"这种兜底。"""
    with pytest.raises(AttachmentError) as exc:
        validate_upload(
            filename="a.pdf", declared_type="application/pdf", content=PNG_HEADER + b"x" * 10
        )
    assert exc.value.code == "MAGIC_BYTES_MISMATCH"


def test_pdf_header_only_is_accepted() -> None:
    """PDF **只查文件头**（计划里第 3 步的取舍）：不解析不可信 PDF —— 解析本身就是攻击面。"""
    result = validate_upload(
        filename="报价单.pdf",
        declared_type="application/pdf",
        content=PDF_HEADER + b"\n1 0 obj\n<<>>\nendobj\n",
    )
    assert result.content_type == "application/pdf"
    assert result.suffix == ".pdf"
    assert result.image_size is None


# ---------------------------------------------------------------- 第 5 步：真解码


def test_fake_png_with_correct_header_is_rejected_by_decoder() -> None:
    """**文件头对、内容假**：这是 magic bytes 挡不住的那一类，只能靠解码器。

    注意这里的替身解码器**不会**因为"开头是 PNG 头"就放行 ——
    否则这条用例测的其实还是第 4 步，第 5 步形同虚设。
    """
    with pytest.raises(AttachmentError) as exc:
        validate_upload(
            filename="图.png",
            declared_type="image/png",
            content=PNG_HEADER + b"not really a png",
            decode_image=fake_png(),
        )
    assert exc.value.code == "IMAGE_DECODE_FAILED"


def test_image_is_rejected_when_decoder_is_unavailable() -> None:
    """没装解码器时**拒绝图片**，而不是跳过校验放行。

    少一道检查的默认结果必须往"更严"那一侧倒 —— 反过来的话，
    依赖出问题的那一刻就是上传口失守的那一刻。
    """
    with pytest.raises(AttachmentError) as exc:
        validate_upload(
            filename="图.png",
            declared_type="image/png",
            content=PNG_HEADER + b"payload",
            decode_image=None,
        )
    assert exc.value.code == "IMAGE_DECODER_UNAVAILABLE"


def test_compression_bomb_is_rejected_by_pixel_count() -> None:
    """压缩炸弹：声明 20000×20000（4 亿像素），字节数却很小。

    上限判定用的是**解码器报出来的尺寸**（PNG 的尺寸在文件头里，几 KB 就能声明很大），
    所以这条检查必须发生在 `load()` 真正吃内存**之前**。
    """
    small_bytes = PNG_HEADER + PNG_TAIL
    assert len(small_bytes) < 1024  # 文件本身很小，骗过了第 2 步
    with pytest.raises(AttachmentError) as exc:
        validate_upload(
            filename="bomb.png",
            declared_type="image/png",
            content=small_bytes,
            decode_image=fake_png((20_000, 20_000)),
        )
    assert exc.value.code == "IMAGE_TOO_LARGE"


def test_pixels_exactly_at_the_limit_are_accepted() -> None:
    """边界闭区间：正好 5000 万像素放行。"""
    side = 5_000  # 5000 × 10000 = 50_000_000
    assert side * 10_000 == MAX_IMAGE_PIXELS
    result = validate_upload(
        filename="ok.png",
        declared_type="image/png",
        content=PNG_HEADER + PNG_TAIL,
        decode_image=fake_png((side, 10_000)),
    )
    assert result.image_size == (side, 10_000)


# ---------------------------------------------------------------- 第 6 步：摘要与元数据


def test_sha256_and_suffix_come_from_the_validated_content() -> None:
    """摘要、后缀、内容类型必须**同源**（都由这一次校验算出来）。

    后缀尤其重要：它由**内容类型**决定，跟用户文件名一点关系都没有 ——
    用户传 `报表.PDF`、`报表`、`报表.pdf` 落在磁盘上都是 `.pdf`。
    """
    body = PDF_HEADER + b"content"
    result = validate_upload(
        filename="报表.PDF", declared_type="application/pdf", content=body
    )
    assert result.sha256 == hashlib.sha256(body).hexdigest()
    assert len(result.sha256) == 64
    assert result.suffix == ".pdf"
    assert result.size_bytes == len(body)


def test_jpeg_alias_is_normalized_before_validation() -> None:
    """`image/jpg` 归一化成 `image/jpeg` 之后，按 jpeg 的文件头校验。"""
    result = validate_upload(
        filename="照片.jpg",
        declared_type="image/jpg",
        # 替身解码器只管 PNG；这里换成"任何字节都认"的那一个，
        # 好让这条用例只验**归一化**这一件事（文件名/JPEG 头由别的用例覆盖）
        content=JPEG_HEADER + b"\x00" * 32,
        decode_image=lambda _content: (10, 10),
    )
    assert result.content_type == "image/jpeg"
    assert result.suffix == ".jpg"


def test_filename_is_required_but_never_used_for_the_suffix() -> None:
    """没有文件名的附件对人没用；但**有**文件名也不会影响落盘后缀。"""
    with pytest.raises(AttachmentError) as exc:
        validate_upload(filename="   ", declared_type="application/pdf", content=PDF_HEADER)
    assert exc.value.code == "MISSING_FILENAME"

    tricky = validate_upload(
        filename="../../../etc/passwd.pdf",
        declared_type="application/pdf",
        content=PDF_HEADER + b"x",
    )
    # 后缀与文件名无关；路径穿越由 object_store 的 UUID 键从字符集上排除
    assert tricky.suffix == ".pdf"


# ---------------------------------------------------------------- 用途标签


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("QUOTATION", "QUOTATION"),
        ("invoice", "INVOICE"),
        (" OTHER ", "OTHER"),
        (None, "OTHER"),
        ("不认识的用途", "OTHER"),
    ],
)
def test_safe_kind_falls_back_to_other(raw, expected) -> None:
    """用途认不出**不报错**（它只是给人分类的标签，不影响安全）。

    与"认不出的文件类型一律拒绝"形成对比：安全相关的判定一律从严，
    纯标签一律从宽 —— 这条界限要一直清楚。
    """
    assert safe_kind(raw) == expected

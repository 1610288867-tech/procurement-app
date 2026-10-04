"""附件的上传校验规则（M9）。**纯函数，不 import 任何框架。**

============================ 这一层为什么必须存在 ============================
上传接口是少数几个"用户能把任意字节送进你系统"的入口。这里每一条校验都对应
一种**不会报错、只会得手**的攻击：

    只信文件名后缀      → 把 `evil.exe` 改名成 `evil.pdf` 就进来了
    只信 Content-Type   → 那是客户端说了算的字段，想写什么写什么
    只信文件头          → 文件头只有几个字节，后面接什么都可以（"文件头对、内容假"）
    不看解压后的尺寸    → 一个 10 MB 的 PNG 可以解出几个 GB 的位图（压缩炸弹）
    用原始文件名存盘    → `../../app/main.py` 直接覆盖源码（路径穿越）

所以这里是**六步校验**，顺序不能调（越便宜的检查越靠前：先看大小，再看类型，
再验文件头，最后才真的解码）：

    1. 申请存在且**在当前用户的可见范围内**（可见范围外 → 404，见 coordinator）
    2. 文件大小 ≤ 10 MB
    3. 定下"按哪种类型校验"：声明了具体类型就用它；声明是空或
       `application/octet-stream`（= "没告诉我"）才用**扩展名**补全；
       补不出来或不在白名单 → 422
    4. 文件头（magic bytes）必须与**第 3 步定下的类型**一致
    5. 图片必须**真的解码一次**（`decode_image` 回调注入，本层不碰解码库）
    6. SHA-256 由调用方算（这里不碰字节之外的任何东西）

第 5 步为什么用**回调**而不是直接依赖 Pillow：`domain/` 不许依赖框架与第三方库，
而且"能不能解码"这件事天生属于基础设施（解码器在 `infrastructure/image_probe.py`）。
这样本模块可以用一个假解码器把六步全测一遍，**毫秒级**。
（措辞上刻意不写成 `import` 的样子：分层自查是一条 grep，
注释里出现那种字样会让"应无输出"的自查多一行噪音。）

============================ 扩展名只补全声明，不代替校验 ============================
第 3 步允许用扩展名补全类型，但**第 4、5 步一步都不会少**：

    扩展名补成 `application/pdf`  →  文件头仍必须是 `%PDF`
    扩展名补成 `image/png`        →  文件头仍必须是 PNG 签名，且必须真的解码成功

也就是说 `evil.exe` 改名成 `evil.pdf` 依然进不来 —— 补全改变的只是
"我们打算按哪种类型去核对内容"，不是"核不核对"。**这两件事一旦混起来，
兜底就变成了绕过**，所以它们在代码里也是分开的两件事：
`infer_type_from_filename` 只回答类型，`_check_magic_bytes` / `_check_image` 只管核对。

============================ 一律拒绝，不做"猜" ============================
除了上面那条明确写死的兜底，没有别的"看起来像 PDF 就当 PDF"：识别不出来就是 422。
上传接口宁可让用户换个文件，也不能收下一个自己都说不清是什么的东西。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass

from app.domain.enums import ATTACHMENT_KINDS, DEFAULT_ATTACHMENT_KIND

#: 单文件上限 10 MB。计划里写死的数字，**不给配置开关**（AGENTS 第 10 条）。
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024

#: 图片解压后的像素上限（约 5000 万像素）。
#: 10 MB 的 PNG 完全可以解出几个 GB 的位图 —— 这条是成本最低的那道防线。
MAX_IMAGE_PIXELS = 50_000_000

#: 内容类型白名单 → 允许的落盘后缀。
#:
#: 后缀由**内容类型**决定，不由用户文件名决定 —— 用户传 `报表.PDF`、
#: `报表.pdf`、`报表` 都落到同一个后缀，磁盘上永远只有三种形状。
ALLOWED_TYPES: dict[str, str] = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
}

#: 内容类型的别名。浏览器对 JPEG 会送 `image/jpg`（非标准但常见），
#: 不归一化就会被白名单挡掉 —— 那是"误伤"而不是"防护"。
CONTENT_TYPE_ALIASES: dict[str, str] = {
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
}

#: 文件头特征（magic bytes）。**这些不是安全边界**，只是便宜的初筛：
#: 真正的判定在第 5 步（内容类型是图片时）。
#:
#: PDF 只查 `%PDF`（4 字节），不解析内容 —— 解析不可信的 PDF 本身就是攻击面。
MAGIC_BYTES: dict[str, tuple[bytes, ...]] = {
    "application/pdf": (b"%PDF",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
}

#: "客户端没告诉我这是什么"的两种声明。
#:
#: `python-multipart` 对没带 `Content-Type` 的 multipart 部分会填
#: `application/octet-stream`；`curl -F`、不少脚本与老客户端对 PDF 也送它。
#: 这两种**不是"声明了别的类型"**，而是"没声明" —— 所以下面允许用扩展名补全。
GENERIC_CONTENT_TYPES: frozenset[str] = frozenset(
    {"", "application/octet-stream", "binary/octet-stream"}
)

#: 扩展名 → 内容类型。**只在声明为 generic 时启用**，而且它只负责"补全声明"。
#:
#: ⚠️ 它**绝不能代替内容校验**：补出来的类型照样要过第 4 步（magic bytes）
#: 与第 5 步（图片真解码）。也就是说 `evil.exe` 改名成 `evil.pdf` 依然进不来 ——
#: 扩展名只影响"我们打算按哪种类型去核对内容"，不影响"核对本身"。
EXTENSION_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


class AttachmentError(ValueError):
    """校验失败。带上 `code` 供边界层转成统一错误体（稳定、可断言）。"""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ValidatedAttachment:
    """校验通过后的**唯一事实**：边界层照着它落库与存盘。

    为什么返回对象而不是让调用方自己再算一遍：内容类型、后缀、大小、摘要
    这四样必须**同源**。分两处算，早晚出现"库里记 pdf、磁盘上是 .png"。
    """

    #: 归一化之后的内容类型（落库、也用于下载响应的 `Content-Type`）
    content_type: str
    #: 落盘后缀（由内容类型决定，**绝不来自用户文件名**）
    suffix: str
    size_bytes: int
    sha256: str
    #: 图片的 (宽, 高)；非图片是 None
    image_size: tuple[int, int] | None
    #: 类型是不是**从扩展名补出来**的（客户端只送了 `application/octet-stream` 或没送）。
    #: 带这个标记是为了让"为什么被判成 PNG"可追溯 —— 落库时也会写进事件快照。
    type_inferred: bool = False


#: 解码器签名：吃字节、返回 (宽, 高)，解不出来就抛异常。
#: 由基础设施层注入（见 `infrastructure/image_probe.py`）。
ImageDecoder = Callable[[bytes], tuple[int, int]]


def normalize_content_type(raw: str | None) -> str:
    """内容类型归一化：去掉参数、转小写、消别名。

    `text/plain; charset=utf-8` 这种带参数的写法要砍掉参数部分，
    否则白名单永远匹配不上 —— 而白名单匹配不上时，开发者很容易
    "顺手"改成 `startswith` 前缀匹配，那就把 `application/pdf.exe` 也放进来了。
    """
    if not raw:
        return ""
    base = raw.split(";", 1)[0].strip().lower()
    return CONTENT_TYPE_ALIASES.get(base, base)


def _check_size(content: bytes) -> None:
    if not content:
        raise AttachmentError("文件是空的", code="EMPTY_FILE")
    if len(content) > MAX_ATTACHMENT_BYTES:
        limit_mb = MAX_ATTACHMENT_BYTES // (1024 * 1024)
        raise AttachmentError(
            f"文件超过 {limit_mb} MB 上限（当前 {len(content) / 1024 / 1024:.1f} MB）",
            code="FILE_TOO_LARGE",
        )


def infer_type_from_filename(filename: str | None) -> str:
    """**只在客户端没声明类型时**用扩展名补全类型；补不出来返回空串。

    ============================ 这条兜底为什么必须存在 ============================
    `application/octet-stream` 不是"另一种类型"，而是**"没告诉我"**：
    `python-multipart` 对没带类型的 multipart 部分填的就是它，
    `curl -F`、不少脚本客户端对 PDF 也送它。把它当"声明了别的类型"拒掉，
    挡掉的是**测试工具和老客户端**，而不是攻击者 —— 攻击者当然会把类型写成
    `application/pdf`（那正是我们想要的诚实声明）。

    ============================ 它为什么不是安全漏洞 ============================
    扩展名只回答"**打算按哪种类型去核对内容**"，不回答"内容是不是这种类型"。
    补出来的类型**照样**要过：

        第 4 步  文件头（magic bytes）必须与它一致
        第 5 步  图片必须真的解码一次

    所以 `evil.exe` 改名成 `evil.pdf` 依然进不来：补成 `application/pdf` 之后，
    文件头 `MZ` 立刻对不上。**拿扩展名换掉内容校验才会出事，这里没有换。**
    """
    if not filename:
        return ""
    stem, dot, suffix = filename.rpartition(".")
    if not dot or not stem:
        # 没有扩展名，或者形如 `.pdf`（只有扩展名、没有主名）
        return ""
    return EXTENSION_TYPES.get(f".{suffix.lower()}", "")


def resolve_content_type(declared: str | None, filename: str | None) -> tuple[str, bool]:
    """定下**这次按哪种类型校验**，返回 `(类型, 是否来自扩展名推断)`。

    三种情况，顺序不能乱：

        1. 客户端声明了**具体**类型        → 用它。声明的类型优先，
          哪怕与扩展名不符（不符会在第 4 步被文件头抓出来）。
        2. 声明是空/`application/octet-stream` → 用扩展名补。
        3. 补不出来                        → 报"不支持的文件类型" 422。

    返回值里带一个"是不是推断来的"标记：边界层可以据此在日志或响应里说清来源，
    排查"为什么这份文件被判成 PNG"时不用猜。
    """
    normalized = normalize_content_type(declared)
    if normalized not in GENERIC_CONTENT_TYPES:
        return normalized, False
    return infer_type_from_filename(filename), True


def _check_content_type(declared: str | None, filename: str | None) -> tuple[str, bool]:
    content_type, inferred = resolve_content_type(declared, filename)
    if not content_type:
        allowed = " / ".join(sorted(EXTENSION_TYPES))
        raise AttachmentError(
            f"无法确定文件类型：既没有可用的 Content-Type，也无法从文件名推断"
            f"（可识别的扩展名：{allowed}）",
            code="MISSING_CONTENT_TYPE",
        )
    if content_type not in ALLOWED_TYPES:
        allowed = " / ".join(sorted(ALLOWED_TYPES))
        raise AttachmentError(
            f"不支持的文件类型：{declared or '（未声明）'}（只接受 {allowed}）",
            code="TYPE_NOT_ALLOWED",
        )
    return content_type, inferred


def _check_magic_bytes(content: bytes, content_type: str) -> None:
    signatures = MAGIC_BYTES.get(content_type, ())
    if signatures and not any(content.startswith(sig) for sig in signatures):
        raise AttachmentError(
            f"文件内容与声明的类型（{content_type}）不符："
            "只改扩展名是骗不过去的，请上传真实的文件",
            code="MAGIC_BYTES_MISMATCH",
        )


def _check_image(content: bytes, content_type: str, decode_image: ImageDecoder | None):
    """第 5 步：图片**真的解码一次**。

    `decode_image` 为 None 时**直接拒绝图片**，而不是"跳过解码放行"：
    少一道检查的默认结果必须是"更严"，不能是"更松"
    （依赖没装好时，接口应当拒绝图片，而不是把没验过的图片收进来）。
    """
    if not content_type.startswith("image/"):
        return None
    if decode_image is None:
        raise AttachmentError(
            "服务器未启用图片解码，无法校验图片附件",
            code="IMAGE_DECODER_UNAVAILABLE",
        )
    try:
        width, height = decode_image(content)
    except AttachmentError:
        raise
    except Exception as exc:  # 解码器抛什么都算"这不是一张合法图片"
        raise AttachmentError(
            "图片文件损坏或格式不合法（解码失败）", code="IMAGE_DECODE_FAILED"
        ) from exc

    if width <= 0 or height <= 0:
        raise AttachmentError("图片尺寸不合法", code="IMAGE_DECODE_FAILED")
    # 压缩炸弹：先看"解出来会占多少"，再决定要不要让解码器吃像素
    if width * height > MAX_IMAGE_PIXELS:
        raise AttachmentError(
            f"图片尺寸过大：{width}×{height}（上限 {MAX_IMAGE_PIXELS // 1_000_000} 百万像素）",
            code="IMAGE_TOO_LARGE",
        )
    return (width, height)


def validate_upload(
    *,
    filename: str | None,
    declared_type: str | None,
    content: bytes,
    decode_image: ImageDecoder | None = None,
) -> ValidatedAttachment:
    """六步校验里的第 2–6 步（第 1 步"申请可见"在 coordinator，要查库）。

    `filename` 参与三件事：判空（没有名字的附件对人没用）、
    **在客户端没声明类型时补全类型**（见 `infer_type_from_filename`）、
    以及落库当元数据。它**绝不参与**落盘路径 —— 磁盘上的名字由
    `ValidatedAttachment.suffix` 决定（见 `infrastructure/object_store.py`）。

    ⚠️ 补全出来的类型只是"打算按哪种类型核对"，第 4 步与第 5 步一步都不会少。
    """
    if not filename or not filename.strip():
        raise AttachmentError("缺少文件名", code="MISSING_FILENAME")

    _check_size(content)
    content_type, inferred = _check_content_type(declared_type, filename)
    _check_magic_bytes(content, content_type)
    image_size = _check_image(content, content_type, decode_image)

    return ValidatedAttachment(
        content_type=content_type,
        suffix=ALLOWED_TYPES[content_type],
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        image_size=image_size,
        type_inferred=inferred,
    )


def safe_kind(raw: str | None) -> str:
    """附件用途：认不出的**不报错**，落到 `OTHER`。

    与"认不出的文件类型一律拒绝"不同 —— 用途只是给人分类的标签，
    它不影响安全；为了一个标签把整次上传打回去，是拿用户的耐心换洁癖。
    """
    if raw and raw.strip().upper() in ATTACHMENT_KINDS:
        return raw.strip().upper()
    return DEFAULT_ATTACHMENT_KIND

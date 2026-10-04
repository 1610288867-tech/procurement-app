"""真解码器的行为测试（M9）—— **真的调 Pillow**。

与 `test_attachment_rules.py` 的分工：那边用替身解码器测**规则**，
这边用真实文件测**解码器到底拦得住什么**。

============================ 这些用例是"实测记录"，不是想象 ============================
写 `infrastructure/image_probe.py` 时我先跑了一遍这几个构造文件，
把结果写进了那里的模块注释。这份测试就是**把那次实测钉死**：
以后有人把 `image.load()` 删掉（觉得 `verify()` 就够了），这里会立刻红。

    PNG 截断一半          → Truncated File Read
    PNG IDAT 被搅乱       → bad header checksum in b'IDAT'
    JPEG 截断一半         → Truncated File Read
    JPEG 尾部被清空       → broken data stream
    PNG 头 + 随机字节     → UnidentifiedImageError

标记是 `unit`：它不连数据库、不碰磁盘，只吃内存里的字节。
"""

from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from app.domain.attachments import MAX_IMAGE_PIXELS, AttachmentError, validate_upload
from app.infrastructure.image_probe import decode_image

pytestmark = pytest.mark.unit


def make_png(size: tuple[int, int] = (40, 30)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, "red").save(buffer, "PNG")
    return buffer.getvalue()


def make_jpeg(size: tuple[int, int] = (40, 30)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, "red").save(buffer, "JPEG")
    return buffer.getvalue()


def test_valid_images_decode_and_report_their_size() -> None:
    assert decode_image(make_png((40, 30))) == (40, 30)
    assert decode_image(make_jpeg((12, 34))) == (12, 34)


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("PNG 截断一半", lambda png, jpg: png[: len(png) // 2]),
        ("PNG IDAT 被搅乱", lambda png, jpg: png[:-20] + bytes(20)),
        ("JPEG 截断一半", lambda png, jpg: jpg[: len(jpg) // 2]),
        ("JPEG 尾部被清空", lambda png, jpg: jpg[:-40] + bytes(40)),
        ("PNG 头 + 随机字节", lambda png, jpg: png[:8] + bytes(200)),
    ],
)
def test_broken_images_are_rejected(name: str, payload) -> None:
    """五类"看起来像图片"的构造文件都必须被拒。

    **只做 `verify()` 会漏掉其中几种**（它不解码像素数据）——
    这条用例就是那句话的证据。

    这里断言"抛异常"而不是某个具体类型：Pillow 对不同损坏方式抛的**不是同一个类**
    （`OSError` / `SyntaxError` / `UnidentifiedImageError`），
    而领域层只认"抛没抛"（全部转成 422）。把类型写死反而会漏掉将来新增的那一种。
    """
    content = payload(make_png(), make_jpeg())
    with pytest.raises((OSError, SyntaxError, ValueError)):
        decode_image(content)


def test_truncated_png_is_rejected_by_the_full_pipeline() -> None:
    """走完整的 `validate_upload`：文件头是**真的** PNG 头（骗过第 4 步），
    但内容被截断 —— 只有第 5 步能拦住它。"""
    truncated = make_png()[: len(make_png()) // 2]
    assert truncated.startswith(b"\x89PNG\r\n\x1a\n")  # 第 4 步会放行
    with pytest.raises(AttachmentError) as exc:
        validate_upload(
            filename="截断.png",
            declared_type="image/png",
            content=truncated,
            decode_image=decode_image,
        )
    assert exc.value.code == "IMAGE_DECODE_FAILED"


def test_real_image_at_the_pixel_limit_is_accepted_by_the_decoder() -> None:
    """上限之内的一张真图：解码器不拦，且报出的尺寸与建图时一致。

    这里**不造 5000 万像素的真图**（那要几百 MB 内存，测试不该这么贵），
    边界判定由 `test_attachment_rules.py` 用替身解码器覆盖；
    这条只验"真解码器在正常尺寸上工作正常"。
    """
    assert decode_image(make_png((100, 100))) == (100, 100)
    assert MAX_IMAGE_PIXELS > 100 * 100


def test_pillow_bomb_threshold_is_above_our_own_limit() -> None:
    """Pillow 自己的闸门必须**高于**本项目的上限。

    否则超限文件会先被 Pillow 抛成 `DecompressionBombError`，
    错误码从"图片尺寸过大"变成一句看不懂的 500 —— 判定入口就散了。
    这条直接读模块被 import 之后的全局值（`image_probe` 在 import 时就设过它）。
    """
    assert Image.MAX_IMAGE_PIXELS is not None
    assert Image.MAX_IMAGE_PIXELS > MAX_IMAGE_PIXELS

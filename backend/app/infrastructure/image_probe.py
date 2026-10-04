"""图片解码探针（M9）—— **全项目唯一 import Pillow 的地方。**

============================ 为什么单独一个文件 ============================
`domain/attachments.py` 需要"这张图片是否真的能解码"这个判断，但 `domain/`
不许依赖第三方库。所以判定留在领域层，**解码动作**作为回调注入进来：

    domain  →  "是不是合法图片？解出来的尺寸是多少？"   （规则、可毫秒级测）
    infra   →  真的把字节交给解码器                    （Pillow 在这一层）

这样领域层的六步校验可以用一个假解码器全部测完，不需要造真的 PNG。

============================ 为什么必须真的解码一次 ============================
文件头只有几个字节，后面接什么都可以。把 `evil.exe` 前面拼上 `\x89PNG...`
就能骗过 magic bytes 那一关 —— 但骗不过解码器：**构造一个能通过完整解码的
任意载荷，比改个后缀难得多。**

`verify()` 与 `load()` 的分工（**两条都要**，本机实测过）：

    verify()  校验文件完整性（CRC / 结构），不解码像素
    load()    真的解码像素 —— 压缩炸弹正是在这一步吃内存

只做 `verify()` 会漏掉"截断的数据流"：PNG 只校验各 chunk 的 CRC，
`verify()` 不读 IDAT；JPEG 的扫描数据更是完全没被 verify 碰过。
下面这些构造文件在**加了 `load()` 之后**才全部被拒（本机实测结果）：

    PNG 截断一半          OSError: Truncated File Read
    PNG IDAT 被搅乱       SyntaxError: bad header checksum in b'IDAT'
    JPEG 截断一半         OSError: Truncated File Read
    JPEG 尾部被清空       OSError: broken data stream when reading image file
    PNG 头 + 随机字节     UnidentifiedImageError: cannot identify image file

============================ 阈值只设一处 ============================
Pillow 自己也有一道压缩炸弹闸（`MAX_IMAGE_PIXELS`，默认 89M，**只警告**，
超两倍才报错）。把它的阈值抬到本项目上限之上，是为了让"超限"这件事
**只有一个判定入口**（`domain/attachments.py` 的 `MAX_IMAGE_PIXELS`）——
否则同一个超限文件可能被 Pillow 先抛成 `DecompressionBombError`，
错误码就从"图片尺寸过大"变成一句看不懂的 500。
"""

from __future__ import annotations

from io import BytesIO

from PIL import Image

from app.domain.attachments import MAX_IMAGE_PIXELS


def decode_image(content: bytes) -> tuple[int, int]:
    """把字节交给解码器，返回 `(宽, 高)`；不是合法图片就抛异常。

    **不做任何"猜格式"的兜底**：`Image.open` 认不出来就抛，
    由领域层统一转成 422（见 `domain/attachments.py` 的 `_check_image`）。
    """
    # 抬到领域层上限之上（+1）：让"超限"由领域层判定，Pillow 不要抢先抛异常
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS + 1

    with Image.open(BytesIO(content)) as image:
        image.verify()  # 结构/CRC 校验：挡住"文件头对、内容假"的构造文件

    # verify() 之后对象不可再用，必须重新打开才能读 size 与 load()
    with Image.open(BytesIO(content)) as image:
        width, height = image.size
        image.load()  # 真的解码一次像素：挡住"截断的数据流"（见模块说明）
    return int(width), int(height)

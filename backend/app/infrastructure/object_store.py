"""本地对象存储（M9）：附件落盘。

============================ 这个文件只有一条铁律 ============================
**磁盘上的文件名永远由服务端生成（UUID），永远不用用户提供的名字。**

用户提供的文件名是不可信输入。把它拼进路径，下面这些就都成立了：

    ../../app/main.py           覆盖源码
    ....//....//etc/passwd      绕过简单的字符串替换
    a/b/c.pdf                   悄悄在别的目录建文件
    CON / NUL / LPT1            Windows 保留设备名，写进去就卡住

uuid4 的十六进制串里只有 `[0-9a-f]`，不含 `/`、`.`、`:`，
**从字符集上就不可能构成路径穿越** —— 这比"过滤危险字符"可靠得多
（黑名单永远漏，白名单永远不漏）。

原始文件名照旧存进数据库当元数据（人需要看到它），
但从"存"到"取"的整条路径上，它一次都不参与文件名拼接。

============================ 两层目录 ============================
`uploads/ab/abcdef…pdf`：用前两个字符做一级分目录。
单目录塞几十万个文件在小文件系统上会明显变慢，分两层就够了，
不需要按日期或按租户分（那是规模上来以后的事）。

============================ 与 `uploads/` 静态目录无关 ============================
**绝不**把 `uploads/` 挂成静态目录（`app.mount(...)`）：那样任何知道文件名的人
都能直接下载，等于绕开全部权限控制。附件只能经 `/api/v1/attachments/{id}/download`
出来，那条路径上有鉴权。
"""

from __future__ import annotations

import uuid
from pathlib import Path


class LocalObjectStore:
    """把字节存到本地目录，返回一个 `storage_key`。

    `storage_key` **不是**文件系统路径，而是"相对这个根目录的键"：
    `ab/abcdef….pdf`。存进库的是它，读的时候再由它拼出真实路径。
    这样将来换成 MinIO / S3 时，库里那一列不用动。
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def new_key(self, suffix: str) -> str:
        """只**生成**键，不落盘。

        为什么要拆成两步（`new_key` + `save`）而不是一个 `save` 直接写完：

            先有键 → 落盘 → 写库；库里那一步失败时，用键把文件删掉

        如果 `save` 自己生成键再返回，调用方在"写库失败"时手里就没有键，
        删不掉那个文件 —— 于是每次失败都留一个孤儿文件，
        而**没人会发现**（磁盘慢慢涨，直到某天有人去数）。

        `save` 也接受外部给的键，这样"回滚"这件事才做得到。
        """
        return f"{uuid.uuid4().hex}{suffix}"

    def save(self, content: bytes, suffix: str, key: str | None = None) -> str:
        """落盘并返回 `storage_key`。`suffix` 由**内容类型**决定（见 domain 层）。"""
        final_key = key or self.new_key(suffix)
        target = self._path_of(final_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return final_key

    def load(self, key: str) -> bytes:
        return self._path_of(key).read_bytes()

    def delete(self, key: str) -> bool:
        """物理删除。**只给"上传失败要回滚"这一种场景用。**

        正常业务里删除附件走软删除（库里标记），保留审计痕迹 ——
        见 `AttachmentRepository.soft_delete` 的说明。
        """
        target = self._path_of(key)
        if not target.exists():
            return False
        target.unlink()
        return True

    def _path_of(self, key: str) -> Path:
        """`storage_key` → 真实路径。

        这里**再拦一次**：`key` 的合法形状是 `<32 位十六进制><.后缀>`，
        不满足就拒绝。理由是"库里那一列也可能被人手改过"——
        运维直接改库不是攻击，但让它演变成路径穿越就是我们的问题。
        """
        stem, _, suffix = key.partition(".")
        if len(stem) != 32 or not all(ch in "0123456789abcdef" for ch in stem):
            raise ValueError(f"非法的 storage_key：{key!r}")
        safe_suffix = f".{suffix}" if suffix and suffix.isalnum() else ""
        return self.root / stem[:2] / f"{stem}{safe_suffix}"

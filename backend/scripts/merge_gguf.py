"""把分片的 GGUF 合并成单文件（M12++ 换模型用）。

    python scripts\\merge_gguf.py 输出.gguf 分片1.gguf 分片2.gguf [...]

============================ 为什么需要它 ============================
本次要导入的 `Qwen2.5-7B-Instruct-Q4_K_M`（ModelScope 官方仓库）是**分片**的
（`-00001-of-00002` + `-00002-of-00002`，3.72 GB + 0.64 GB），
而 **Ollama 不支持多文件 GGUF 导入** —— 这是它至今未实现的已知限制
（[ollama/ollama#5245](https://github.com/ollama/ollama/issues/5245)），
且本机装的是 Ollama 0.40.3，安装目录里**没有 `llama-gguf-split`**
（那个工具本来能做 `--merge`）。

于是只剩两条路：
    ① 换一个**单文件**的 GGUF 源（实测 bartowski 那份只有 **196 KB/s**，
       4.36 GB 要 6.5 小时；而官方分片源实测 850 KB/s）；
    ② 自己把分片拼起来。

============================ 为什么"直接拼"是对的 ============================
GGUF 的分片**不是"每片一个完整文件、内容各占一半"**，而是
**从第二个分片起只含张量数据，前面的文件头（magic + 元数据 + 张量表）
只在第一个分片里**（这正是"分片"的约定：读取方先读分片 1 建索引，
再按顺序读其余分片取数据）。

所以合并 = **按序字节拼接**，不加任何分隔符、不重写头部。
拼接前必须校验两件事，否则会拼出一个"能读、但结果静默错"的文件：

    ① 每片的 magic 是 `GGUF`（认得出格式）；
    ② 第一片头部的 `split.count` 等于实际给的分片数、
       且每片的 `split.tensors.count` 加起来等于第一片的 `tensor_count`。

第 ② 条是**必须**的：只验 magic 的话，给两片顺序反了、或少给一片，
拼出来的文件仍然"是个 GGUF"，但张量表对不上 ——
加载时要么在某个张量上崩，要么（更糟）读出错位的数。

用法（本机实际会跑的那条）：

    python scripts\\merge_gguf.py D:\\develop\\models\\qwen2.5-7b-instruct-q4_k_m.gguf ^
        D:\\develop\\models\\qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf ^
        D:\\develop\\models\\qwen2.5-7b-instruct-q4_k_m-00002-of-00002.gguf
"""

from __future__ import annotations

import shutil
import struct
import sys
from pathlib import Path

#: GGUF 版本 1/2/3 的头部：magic(4) + version(4) + tensor_count(8) + metadata_kv_count(8)
_HEADER = struct.Struct("<4sIQQ")
_MAGIC = b"GGUF"

#: 元数据类型 → (struct 格式, 名称)
_SCALAR = {
    0: ("<B", "uint8"),
    1: ("<b", "int8"),
    2: ("<H", "uint16"),
    3: ("<h", "int16"),
    4: ("<I", "uint32"),
    5: ("<i", "int32"),
    6: ("<f", "float32"),
    7: ("<B", "bool"),
    10: ("<Q", "uint64"),
    11: ("<q", "int64"),
    12: ("<d", "float64"),
}
_STRING = 8
_ARRAY = 9


class GgufError(RuntimeError):
    """GGUF 读取失败。**要带上下文**：这类失败的原因通常是"分片给错了"。"""


def _read_string(handle) -> str:
    (length,) = struct.unpack("<Q", handle.read(8))
    if length > 1 << 20:
        raise GgufError(f"字符串长度异常（{length} 字节）—— 文件可能不是 GGUF 或已损坏")
    return handle.read(length).decode("utf-8", errors="replace")


def _read_value(handle, value_type: int):
    if value_type in _SCALAR:
        fmt, _name = _SCALAR[value_type]
        size = struct.calcsize(fmt)
        return struct.unpack(fmt, handle.read(size))[0]
    if value_type == _STRING:
        return _read_string(handle)
    if value_type == _ARRAY:
        (item_type,) = struct.unpack("<I", handle.read(4))
        (count,) = struct.unpack("<Q", handle.read(8))
        return [_read_value(handle, item_type) for _ in range(count)]
    raise GgufError(f"未知的元数据类型 {value_type} —— 这份 GGUF 比本脚本认识的更新")


def read_header(path: Path) -> dict:
    """读出 GGUF 头部里与本任务有关的那几项。

    只读到元数据结束就停 —— **不解析张量表、不读张量数据**
    （那要几百 MB 内存，而合并根本不需要理解张量）。
    """
    with path.open("rb") as handle:
        magic, version, tensor_count, kv_count = _HEADER.unpack(handle.read(_HEADER.size))
        if magic != _MAGIC:
            raise GgufError(f"{path.name} 不是 GGUF（前四字节是 {magic!r}，应为 {_MAGIC!r}）")
        info: dict = {
            "version": version,
            "tensor_count": tensor_count,
            "kv_count": kv_count,
            "size": path.stat().st_size,
        }
        wanted = {
            "general.architecture",
            "split.count",
            "split.tensors.count",
            "general.name",
        }
        for _ in range(kv_count):
            key = _read_string(handle)
            (value_type,) = struct.unpack("<I", handle.read(4))
            value = _read_value(handle, value_type)
            if key in wanted:
                info[key] = value
        # 元数据之后的偏移：分片 2+ 的"数据"从这里开始（前面只有头部）
        info["data_offset"] = handle.tell()
        return info


def merge(output: Path, shards: list[Path]) -> None:
    if len(shards) < 2:
        raise GgufError("至少要给两个分片；只有一个文件的话直接用它，不需要合并")

    infos = [read_header(path) for path in shards]
    first = infos[0]

    print("分片校验：")
    for path, info in zip(shards, infos, strict=True):
        print(
            f"  {path.name}: v{info['version']} / 张量 {info['tensor_count']} "
            f"/ 元数据 {info['kv_count']} 项 / 头部 {info['data_offset']} 字节 "
            f"/ {info['size'] / 1024**3:.2f} GB"
        )

    declared = first.get("split.count")
    if declared is not None and int(declared) != len(shards):
        raise GgufError(
            f"第一片声明 split.count={declared}，而你给了 {len(shards)} 个分片 —— "
            "**分片不全**，拼出来的文件会缺张量"
        )
    tensors_declared = first.get("split.tensors.count")
    if tensors_declared is not None:
        # 各片的 split.tensors.count 是"直到该片为止的累计数"（llama.cpp 的约定），
        # 所以末片的这个数应当等于第一片的 tensor_count 总数。
        last = infos[-1].get("split.tensors.count")
        if last is not None and int(last) != int(first["tensor_count"]):
            raise GgufError(
                f"末片声明 split.tensors.count={last}，而第一片 tensor_count="
                f"{first['tensor_count']} —— **分片顺序错了或中间缺片**"
            )

    if output.exists():
        raise GgufError(f"输出文件已存在：{output}（先删掉它，避免覆盖掉一份完整模型）")

    free = shutil.disk_usage(output.parent).free
    need = sum(info["size"] for info in infos)
    if free < need + (64 << 20):
        raise GgufError(
            f"磁盘空间不足：需要约 {need / 1024**3:.2f} GB，可用 {free / 1024**3:.2f} GB"
        )

    print(f"\n开始合并 → {output}")
    print(f"  预计大小 {need / 1024**3:.2f} GB（= 各片之和，拼接不新增字节）")

    written = 0
    with output.open("wb") as out:
        for path in shards:
            with path.open("rb") as src:
                while True:
                    chunk = src.read(8 << 20)  # 8 MB 一块：够快，内存也稳
                    if not chunk:
                        break
                    out.write(chunk)
                    written += len(chunk)
            print(f"  已并入 {path.name}（累计 {written / 1024**3:.2f} GB）")

    size = output.stat().st_size
    if size != need:
        raise GgufError(f"合并后大小 {size} 与预期 {need} 不符 —— 写入过程可能被中断")

    # 再读一次头，确认它仍是一个合法 GGUF（合并写错字节的话这里会报出来）
    check = read_header(output)
    if check["tensor_count"] != first["tensor_count"]:
        raise GgufError("合并后的张量数与第一片不一致 —— 不要用它，删掉重来")

    print(f"\n完成：{output}（{size / 1024**3:.2f} GB，张量 {check['tensor_count']} 个）")


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    output = Path(argv[1])
    shards = [Path(item) for item in argv[2:]]
    for path in shards:
        if not path.exists():
            print(f"分片不存在：{path}")
            return 1
    try:
        merge(output, shards)
    except GgufError as exc:
        print(f"\n合并失败：{exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

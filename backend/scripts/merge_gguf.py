"""把分片的 GGUF 合并成单文件（M12++ 换模型用）。

    python scripts\\merge_gguf.py 输出.gguf 分片1.gguf 分片2.gguf [...]

============================ 为什么需要它 ============================
要导入的 `Qwen2.5-7B-Instruct-Q4_K_M`（ModelScope 官方仓库）是**分片**的
（`-00001-of-00002` + `-00002-of-00002`），而 **Ollama 不支持多文件 GGUF 导入**
（[ollama/ollama#5245](https://github.com/ollama/ollama/issues/5245)，
本机 Ollama 0.40.3 安装目录里也没有 `llama-gguf-split`）。

============================ 第一版做错过，教训在算法里 ============================
第一版以为"分片 = 一个文件被切成两半"，于是**按字节拼接**
（片1 全要 + 跳过片2 的头部再拼）。实测 Ollama 加载时报：

    error loading model: check_tensor_dims: tensor 'output_norm.weight' not found

**根因（诊断了两片的真实布局）**：

    片1：张量 280 个，**自带一张表**，offset 各自相对本片数据区
    片2：张量  59 个，**也自带一张表**，offset 也各自相对本片数据区
    两片的张量名**交集为 0**（280 + 59 = 339 = 末片声明的累计数）

也就是说**分片不是字节切分，而是"按张量分桶"**：每片都有一张
"我这一片有哪些张量、它们在本片的什么位置"的表，数据**只在本片内连续**。
第一版把片2 的**表**当成了数据写进去，于是那 59 个张量（含 `output_norm.weight`）
**压根没有被声明**，文件自然加载失败。

============================ 正确算法 ============================
    ① 读每片的元数据 + **完整张量表**
    ② 合并元数据：去掉 `split.count` / `split.tensors.count`
       （Ollama 见到 `split.*` 就要求文件名符合 llama.cpp 分片命名）
    ③ 合并张量表：各片按顺序拼接，`offset` 由我们自己**重算**
    ④ 重排数据：对每条张量按 `ggml_type` 算出字节数，从**它本片的位置**读出，
       写到新文件的新偏移（按 `general.alignment` 对齐）
    ⑤ 复核：新文件能被重新解析、张量数 = 各片之和、每条张量的声明范围都在文件内

第 ④ 步是这个脚本的**全部意义** —— 它必须理解"张量的字节数由形状与类型算出"。
写错的话文件"看起来是 GGUF"（头部解析得动），但张量数据错位，
要到加载时才在某个张量上报找不到或尺寸不符。
"""

from __future__ import annotations

import contextlib
import shutil
import struct
import sys
from pathlib import Path

# ============================ 输出必须是 UTF-8（否则这个脚本会崩）============================
# 本脚本用了 `✅`（U+2705）做对账标记，而 Windows 上 Python 的 stdout
# **默认按控制台代码页编码**。在 `chcp 936`（GBK）终端里它编不出来，
# 报 `UnicodeEncodeError`，而且**是在校验通过之后、写文件之前**抛 ——
# 看起来像"合并失败"，其实一个字节都还没写。
#
# ⚠️ 同一个坑在本项目已踩过**两次**（`knowledge_eval.py` 的报告符号、这里）。
# 所以修法不是"把 ✅ 换成 ASCII"，而是**每个往终端打字的脚本都加这一段**。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        with contextlib.suppress(OSError, ValueError):
            _stream.reconfigure(encoding="utf-8", errors="replace")

#: GGUF 头部：magic(4) + version(4) + tensor_count(8) + metadata_kv_count(8)
_HEADER = struct.Struct("<4sIQQ")
_MAGIC = b"GGUF"

#: 合并成单文件时必须**删掉**的元数据键。
#:
#: Ollama 只要看到 `split.count` 就认定这是"分片文件"，并要求文件名符合
#: llama.cpp 的分片命名（`*-00001-of-00002.gguf`）—— 而合并件的名字里没有那一段，
#: 于是直接拒绝：`invalid split GGUF: … must use llama.cpp split filename pattern`
#: （实测撞到）。**结构上它已经是单文件了**，所以正确的做法是让元数据也如实反映
#: 这一点，而不是把名字改成假装还是分片。
_SPLIT_KEYS = frozenset({"split.count", "split.tensors.count"})

#: 元数据标量类型 → (struct 格式, 字节数)
_SCALAR = {
    0: ("<B", 1),
    1: ("<b", 1),
    2: ("<H", 2),
    3: ("<h", 2),
    4: ("<I", 4),
    5: ("<i", 4),
    6: ("<f", 4),
    7: ("<B", 1),
    10: ("<Q", 8),
    11: ("<q", 8),
    12: ("<d", 8),
}
_STRING = 8
_ARRAY = 9

#: `ggml_type` → (每块字节数, 每块元素数)，取自 llama.cpp 的 `ggml.h`。
#: **不认识的类型一律拒绝合并**，而不是猜一个大小 —— 猜错的后果是数据整体错位，
#: 而文件头仍然合法，那正是最难查的一类错。
_BLOCK = {
    0: (4, 1),  # F32
    1: (2, 1),  # F16
    2: (18, 32),  # Q4_0
    3: (20, 32),  # Q4_1
    6: (22, 32),  # Q5_0
    7: (24, 32),  # Q5_1
    8: (34, 32),  # Q8_0
    9: (40, 32),  # Q8_1
    10: (84, 256),  # Q2_K
    11: (110, 256),  # Q3_K
    12: (144, 256),  # Q4_K
    13: (176, 256),  # Q5_K
    14: (210, 256),  # Q6_K
    15: (292, 256),  # Q8_K
    16: (66, 256),  # IQ2_XXS
    17: (74, 256),  # IQ2_XS
    18: (98, 256),  # IQ3_XXS
    19: (42, 256),  # IQ1_S
    20: (18, 32),  # IQ4_NL
    21: (110, 256),  # IQ3_S
    22: (82, 256),  # IQ2_S
    23: (34, 32),  # IQ4_XS
    24: (1, 1),  # I8
    25: (2, 1),  # I16
    26: (4, 1),  # I32
    27: (8, 1),  # I64
    28: (8, 1),  # F64
    29: (56, 256),  # IQ1_M
    30: (2, 1),  # BF16
    34: (18, 256),  # TQ1_0
    35: (18, 256),  # TQ2_0
    39: (17, 32),  # MXFP4
}


class GgufError(RuntimeError):
    """GGUF 处理失败。**要带上下文**：这类失败的原因通常是"分片给错了"。"""


def _align(offset: int, alignment: int) -> int:
    return offset + (alignment - (offset % alignment)) % alignment


def _read_string(handle) -> str:
    (length,) = struct.unpack("<Q", handle.read(8))
    if length > 1 << 20:
        raise GgufError(f"字符串长度异常（{length} 字节）—— 文件可能不是 GGUF 或已损坏")
    return handle.read(length).decode("utf-8", errors="replace")


def _read_value(handle, value_type: int):
    if value_type in _SCALAR:
        fmt, _size = _SCALAR[value_type]
        return struct.unpack(fmt, handle.read(struct.calcsize(fmt)))[0]
    if value_type == _STRING:
        return _read_string(handle)
    if value_type == _ARRAY:
        (item_type,) = struct.unpack("<I", handle.read(4))
        (count,) = struct.unpack("<Q", handle.read(8))
        for _ in range(count):
            _read_value(handle, item_type)
        return None
    raise GgufError(f"未知的元数据类型 {value_type} —— 这份 GGUF 比本脚本认识的更新")


def _read_metadata_raw(handle) -> list[tuple[str, bytes]]:
    """读出全部元数据项，每项**原样保留字节**（`key` 之外的整段）。

    值以原始字节保留：合并时要删掉两个键并重写头部，
    而**重编码值有风险**（字符串长度前缀、数组元素数、浮点表示都可能写错），
    保留字节就完全不用理解它们。
    """
    # ⚠️ **本函数自己 seek 到文件开头**，不假定调用方的指针在哪。
    # 第一版假定"指针在 0"，而调用方 `Shard.__init__` 是**先读了头部**（指针在 24）
    # 再调它 —— 于是元数据从第 48 字节开始读，读进了张量数据里，报
    # `字符串长度异常（36061738356 字节）`。
    # **同一个文件、同一段逻辑，我的诊断脚本读得动、正式脚本读不动** ——
    # 差别就在这一句 seek。
    handle.seek(0)
    items: list[tuple[str, bytes]] = []
    (_magic, _version, _tensors, kv_count) = _HEADER.unpack(handle.read(_HEADER.size))
    for _ in range(kv_count):
        key = _read_string(handle)
        start = handle.tell()
        (value_type,) = struct.unpack("<I", handle.read(4))
        _read_value(handle, value_type)  # 读过去，只为定位结束位置
        end = handle.tell()
        handle.seek(start)
        items.append((key, handle.read(end - start)))
    return items


def _tensor_bytes(dims: tuple[int, ...], ggml_type: int) -> int:
    """一条张量的数据字节数 = 元素总数 ÷ 每块元素数 × 每块字节数。"""
    spec = _BLOCK.get(ggml_type)
    if spec is None:
        raise GgufError(
            f"不认识的 ggml_type={ggml_type} —— **拒绝猜大小**（猜错会让数据整体错位，"
            "而文件头仍然合法）。请改用能识别该类型的工具（如 llama.cpp 的 gguf-split）"
        )
    block_bytes, block_elems = spec
    total = 1
    for dim in dims:
        total *= int(dim)
    if total % block_elems:
        raise GgufError(f"元素数 {total} 不是块大小 {block_elems} 的整数倍（type={ggml_type}）")
    return total // block_elems * block_bytes


class Shard:
    """一个分片：元数据、张量表、数据区起点。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.size = path.stat().st_size
        with path.open("rb") as handle:
            magic, version, tensor_count, _kv = _HEADER.unpack(handle.read(_HEADER.size))
            if magic != _MAGIC:
                raise GgufError(f"{path.name} 不是 GGUF（前四字节 {magic!r}，应为 {_MAGIC!r}）")
            self.version = version
            self.metadata = _read_metadata_raw(handle)
            self.tensors: list[tuple[str, tuple[int, ...], int, int]] = []
            for _ in range(tensor_count):
                name = _read_string(handle)
                (n_dims,) = struct.unpack("<I", handle.read(4))
                dims = struct.unpack("<" + "Q" * n_dims, handle.read(8 * n_dims))
                (ggml_type,) = struct.unpack("<I", handle.read(4))
                (offset,) = struct.unpack("<Q", handle.read(8))
                self.tensors.append((name, dims, ggml_type, offset))
            table_end = handle.tell()
        self.alignment = int(self.meta_int("general.alignment", 32))
        self.data_start = _align(table_end, self.alignment)
        # 表里声明的范围不能超出文件（超出说明分片截断或给错了）。
        # ⚠️ 这一步是**便宜的完整性检查**：它能在合并前就发现"某片下了一半"。
        for name, dims, ggml_type, offset in self.tensors:
            need = offset + _tensor_bytes(dims, ggml_type)
            if self.data_start + need > self.size:
                raise GgufError(
                    f"{self.path.name} 里的张量 {name!r} 声明到 "
                    f"{self.data_start + need} 字节，而文件只有 {self.size} 字节 —— "
                    "**分片不完整**"
                )

    def meta_int(self, key: str, default: int) -> int:
        for item_key, raw in self.metadata:
            if item_key == key:
                (value_type,) = struct.unpack("<I", raw[:4])
                fmt, _size = _SCALAR[value_type]
                return int(struct.unpack(fmt, raw[4:])[0])
        return default


def merge(output: Path, shards: list[Path]) -> None:
    if len(shards) < 2:
        raise GgufError("至少要给两个分片；只有一个文件的话直接用它，不需要合并")

    parts = [Shard(path) for path in shards]
    first = parts[0]

    print("分片校验：")
    total_tensors = 0
    for part in parts:
        total_tensors += len(part.tensors)
        print(
            f"  {part.path.name}: v{part.version} / 张量 {len(part.tensors)} "
            f"/ 元数据 {len(part.metadata)} 项 / 数据起点 {part.data_start} "
            f"/ {part.size / 1024**3:.2f} GB"
        )

    # ---- 分片完整性：名称不能重复、片数与累计数要对账 ----
    seen: set[str] = set()
    for part in parts:
        for name, *_rest in part.tensors:
            if name in seen:
                raise GgufError(f"张量 {name!r} 在多片里重复出现 —— **分片给重了或顺序不对**")
            seen.add(name)
    declared = first.meta_int("split.count", 0)
    if declared and declared != len(parts):
        raise GgufError(
            f"第一片声明 split.count={declared}，而你给了 {len(parts)} 个分片 —— **分片不全**"
        )
    cumulative = first.meta_int("split.tensors.count", 0)
    if cumulative:
        # ⚠️ 各片的 `split.tensors.count` 是"到该片为止的**累计**数"，不是该片自己的数量。
        # 实测：片1 = 280（tensor_count 也是 280）、片2 = 339（280+59）。
        # 第一版把末片的累计值当成"该片自己的数量"去和第一片比，于是对**完全正确**的
        # 分片报了"顺序错了或缺片"。误报本身证明这道校验会拦人，只是拦错了方向。
        last_cumulative = parts[-1].meta_int("split.tensors.count", 0)
        if last_cumulative and last_cumulative != total_tensors:
            raise GgufError(
                f"末片声明累计张量 {last_cumulative}，各片相加是 {total_tensors} —— "
                "**分片顺序错了或中间缺片**"
            )
        print(f"  对账：Σ各片张量 {total_tensors} == 末片累计 {last_cumulative}  ✅")
    else:
        print(f"  对账：各片张量相加 {total_tensors}（没有 split.tensors.count 可对）")

    # ---- 合并元数据：删掉分片标记 ----
    kept = [(key, raw) for key, raw in first.metadata if key not in _SPLIT_KEYS]
    dropped = [key for key, _raw in first.metadata if key in _SPLIT_KEYS]
    print(f"  头部元数据：{len(first.metadata)} 项，删掉分片标记 {dropped or '（无）'}")

    # ---- 合并张量表（保留各条在本片的旧 offset，搬数据时要用）----
    new_table: list[tuple[tuple[str, tuple[int, ...], int, int], int]] = []
    for index, part in enumerate(parts):
        for item in part.tensors:
            new_table.append((item, index))

    if output.exists():
        raise GgufError(f"输出文件已存在：{output}（先删掉它，避免覆盖掉一份完整模型）")

    # ---- 算新布局：表大小 → 数据区起点 → 每条张量的新偏移 ----
    meta_bytes = sum(8 + len(key.encode("utf-8")) + len(raw) for key, raw in kept)
    table_bytes = 0
    for (name, dims, _t, _o), _src in new_table:
        table_bytes += 8 + len(name.encode("utf-8")) + 4 + 8 * len(dims) + 4 + 8
    data_start = _align(_HEADER.size + meta_bytes + table_bytes, first.alignment)

    offsets: list[int] = []
    cursor = 0
    for (name, dims, ggml_type, _o), _src in new_table:
        offsets.append(cursor)
        cursor = _align(cursor + _tensor_bytes(dims, ggml_type), first.alignment)
    need = data_start + cursor

    free = shutil.disk_usage(output.parent).free
    if free < need + (64 << 20):
        raise GgufError(
            f"磁盘空间不足：需要约 {need / 1024**3:.2f} GB，可用 {free / 1024**3:.2f} GB"
        )

    print(f"\n开始合并 → {output}")
    print(
        f"  张量 {len(new_table)} 个 / 数据区起点 {data_start} "
        f"/ 预计 {need / 1024**3:.2f} GB"
    )

    with output.open("wb") as out:
        out.write(_HEADER.pack(_MAGIC, first.version, len(new_table), len(kept)))
        for key, raw in kept:
            encoded = key.encode("utf-8")
            out.write(struct.pack("<Q", len(encoded)))
            out.write(encoded)
            out.write(raw)
        for ((name, dims, ggml_type, _o), _src), offset in zip(
            new_table, offsets, strict=True
        ):
            encoded = name.encode("utf-8")
            out.write(struct.pack("<Q", len(encoded)))
            out.write(encoded)
            out.write(struct.pack("<I", len(dims)))
            for dim in dims:
                out.write(struct.pack("<Q", int(dim)))
            out.write(struct.pack("<I", ggml_type))
            out.write(struct.pack("<Q", offset))
        pad = data_start - out.tell()
        if pad < 0:
            raise GgufError(f"头部比预算大了 {-pad} 字节 —— 布局算错了，别用这个输出")
        out.write(b"\x00" * pad)

        # ---- 数据：逐片、逐张量搬到新位置（**这一步不能省**）----
        for index, part in enumerate(parts):
            moved = 0
            with part.path.open("rb") as src:
                for local, (name, dims, ggml_type, old_offset) in enumerate(part.tensors):
                    global_index = sum(len(p.tensors) for p in parts[:index]) + local
                    size = _tensor_bytes(dims, ggml_type)
                    src.seek(part.data_start + old_offset)
                    blob = src.read(size)
                    if len(blob) != size:
                        raise GgufError(
                            f"{part.path.name} 的 {name!r} 只读到 {len(blob)} 字节（应 {size}）"
                        )
                    out.seek(data_start + offsets[global_index])
                    out.write(blob)
                    moved += size
            print(f"  已搬入 {part.path.name}（{moved / 1024**3:.2f} GB 张量数据）")

        out.truncate(need)

    # ---- 复核：新文件能被解析、张量数对、无分片标记 ----
    merged = Shard(output)
    if len(merged.tensors) != total_tensors:
        raise GgufError(f"合并后张量 {len(merged.tensors)} 个，应为 {total_tensors} —— 不要用它")
    if any(key in _SPLIT_KEYS for key, _raw in merged.metadata):
        raise GgufError("合并后仍带 `split.*` —— Ollama 会拒收它")

    print(
        f"\n完成：{output}（{output.stat().st_size / 1024**3:.2f} GB，"
        f"张量 {len(merged.tensors)} 个，已无分片标记）"
    )
    print(
        f"  抽样：{merged.tensors[0][0]} … {merged.tensors[-1][0]} "
        "的声明范围都在文件内（否则上面构造函数就会报）"
    )


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

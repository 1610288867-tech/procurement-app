# history · 有价值的旧过程归档

> **用途**：需要追溯时**先搜索再读**，不要顺序读。
>
> ⚠️ **这些文件是"当时的样子"，不再维护** —— 它们保留了阶段过程中的完整细节
> （逐条验收证据、排查过程、当时的数字）。**当前状态不在这些文件里**：
> 现在看 [NOW.md](../../NOW.md)，阶段验收看 [PROGRESS.md](../../PROGRESS.md)，
> 决策看 [DECISIONS.md](../../DECISIONS.md)，风险看 [RISKS.md](../../RISKS.md)。
>
> 最后核实：2026-10-15

## 为什么保留它们

[面试.md](../interview/面试.md) 大量引用这些过程细节
（"当时实测撞到什么""报错原文是什么""最后是怎么定位的"），那是面试时最有说服力的部分，
必须**有据可查**。所以压缩当前文档时，原文一并归档，不删除。

[面试素材-2026-10-15.md](面试素材-2026-10-15.md) 是另一类归档：它**自己就是聚合层**
（把 PROGRESS / Development progress / README 的数字抄一遍），
263 个事实指纹里与 [面试.md](../interview/面试.md) 重叠 **40%**、
与 [PROGRESS.md](../../PROGRESS.md) **41%**、与 [Development progress.md](../../Development%20progress.md) **50%**，
而且数字已过期（正文写 `544 passed`，实际 **680 passed**）。
其中**全文独有**的两节（C1–C28 矛盾表、盲点清单）已抽成
[遗留矛盾与盲点.md](../domain/遗留矛盾与盲点.md) 继续维护，其余随原文冻结。

## 归档清单

| 文件 | 原位置 | 归档时间 | 内容 | 为什么归档 |
| --- | --- | --- | --- | --- |
| `PROGRESS-M0-M11-全文-2026-10-14.md` | `PROGRESS.md`（1415 行） | 2026-10-14 | M0–M12 各阶段的**逐条验收证据** | 当前 `PROGRESS.md` 只保留"逐阶段验收总表 + 近期阶段"，早期阶段的逐条明细移到这里 |
| `Development-progress-M0-M11-全文-2026-10-14.md` | `Development progress.md`（1598 行） | 2026-10-14 | M0–M12 各阶段的**四问原文** | 同上：当前文档只留每阶段四问的精华，原文归档 |
| `面试素材-2026-10-15.md` | `docs/interview/面试素材.md`（950 行） | 2026-10-15 | 项目细节与数字的**聚合稿**（含 C1–C28 矛盾表与盲点清单原文） | 它是**派生聚合层**：与 `面试.md` 重叠 40%、与 `PROGRESS` 41%、与 `Development progress` 50%，且数字已过期（544 → 680）。**独有两节已抽到 [遗留矛盾与盲点.md](../domain/遗留矛盾与盲点.md)** |

## 检索建议

先按关键词搜索，再读命中片段及其上下文（**不要顺序读**）：

```powershell
# 例：找"条件更新之后回读"这类问题的全部现场
Get-ChildItem docs\history -Filter *.md | Select-String -Pattern "populate_existing"

# 例：找某次实测报错的原因
Get-ChildItem docs\history -Filter *.md | Select-String -Pattern "MissingGreenlet"

# 例：找某个阶段的测试条数演进
Get-ChildItem docs\history -Filter *.md | Select-String -Pattern "passed"
```

## ⚠️ 归档文件里的相对链接**是断的**（预期的）

归档原文是从仓库根复制过来的，里面的链接按**老位置**（仓库根）写，
例如 `[PROGRESS.md](PROGRESS.md)` 或 `docs/采购内控对照.md` —— 从 `docs/history/` 解析当然不存在。

**这是归档的结构性后果，不是错误**，而且**刻意不改**：

- 它们已经冻结，改了就破坏了"当时的样子"；
- 要读哪份，按**文件名**直接去当前位置找（`PROGRESS.md` 在仓库根、`docs/X.md` 在 `docs/`）；
- 用 `scripts\检查文档链接.mjs` 巡检时会报出它们 —— **那 12 处是已知且预期的**
  （该脚本只跳过 `docs/history/`，其余 0 断链才算过）。

## 维护规则

- **发生必要归档时更新**本文件（加一行：文件、原位置、时间、内容、为什么）
- 归档文件**不再修改**；要更正错误，改当前文档并在本表注明
- 不把"完整聊天记录"或"操作流水账"归档进来 —— 按 [AGENTS.md](../../AGENTS.md) 第三章的写入规则，
  那类内容不保存

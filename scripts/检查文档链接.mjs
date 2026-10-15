/**
 * 全仓 markdown 链接巡检（只读，不修改任何文件）。
 *
 * 用途：本次规范化移动了一批文档，需要确认没有留下断链。
 * 规则：
 *   · 跳过 http(s):// 与 mailto: 等外部链接
 *   · 跳过纯锚点（#...）
 *   · 相对链接按**所在文件的目录**解析，检查目标是否存在
 *   · URL 里的 %20 等编码先解码（`Development%20progress.md`）
 *   · **归档目录 `docs/history/` 默认跳过**：那里的原文是从仓库根复制的，
 *     链接按老位置写、**刻意不改**（它们是冻结的快照）。这是已知且预期的，
 *     把它的断链混进结果会让真正的断链看不见 —— 见 `docs/history/README.md`。
 *     想连它一起查：加 `--with-history`。
 */
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";

const ROOT = process.cwd();
const WITH_HISTORY = process.argv.includes("--with-history");
const SKIP_DIRS = new Set(["node_modules", ".git", ".venv", ".ollama", "dist", ".mypy_cache", ".pytest_cache", ".ruff_cache"]);
if (!WITH_HISTORY) SKIP_DIRS.add("history");

function walk(dir, out = []) {
  for (const entry of readdirSync(dir)) {
    if (SKIP_DIRS.has(entry)) continue;
    const full = join(dir, entry);
    const st = statSync(full);
    if (st.isDirectory()) walk(full, out);
    else if (entry.endsWith(".md")) out.push(full);
  }
  return out;
}

const files = walk(ROOT);
let checked = 0;
const broken = [];

for (const file of files) {
  const text = readFileSync(file, "utf8");
  const dir = dirname(file);
  // [label](target)
  for (const m of text.matchAll(/\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g)) {
    const raw = m[2];
    if (/^(https?:|mailto:|tel:|#)/i.test(raw)) continue;
    const target = decodeURIComponent(raw.split("#")[0]);
    if (!target) continue;
    checked += 1;
    const abs = target.startsWith("/") ? resolve(ROOT, "." + target) : resolve(dir, target);
    if (!existsSync(abs)) {
      broken.push({ file: file.replace(ROOT + "\\", ""), target: raw });
    }
  }
}

console.log(`检查 ${files.length} 个 markdown 文件，${checked} 个内部链接`);
if (broken.length === 0) {
  console.log("✅ 无断链");
} else {
  console.log(`❌ 断链 ${broken.length} 处：`);
  for (const b of broken) console.log(`   ${b.file}  →  ${b.target}`);
}
process.exit(broken.length === 0 ? 0 : 1);

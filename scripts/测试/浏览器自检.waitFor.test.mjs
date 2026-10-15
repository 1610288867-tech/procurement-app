/**
 * `waitFor` 的重试语义测试。
 *
 * ============================ 为什么这段必须被测试 ============================
 * 这次改动给 `waitFor` 加了容错（求值抛错 → 继续轮询），它修的是一个真实竞态：
 * `Page.navigate` 拆掉旧 document 后、新 document 的 `<body>` 挂上之前求值，
 * `document.body` 是 `null` → TypeError → **一次瞬时抖动终结整轮自检**
 * （实测连撞两次，列号反解定位到行 357）。
 *
 * ⚠️ **但容错有个致命的反方向风险**：写错了会变成"**失败也照样通过**"，
 * 那比原来的偶发中断更糟 —— 中断至少是响的，而假通过是哑的。
 * 所以下面每一条都在守这件事。
 *
 * 用手写的假 `page`（只有 `evaluate`），不需要 Edge、毫秒级。
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { waitFor } from "../浏览器自检.mjs";

/** 假页面：`behaviour` 决定每次 `evaluate` 的结果。 */
function fakePage(behaviour) {
  let calls = 0;
  return {
    get calls() {
      return calls;
    },
    async evaluate() {
      calls += 1;
      return behaviour(calls);
    },
  };
}

test("条件一开始就成立 → 立即返回，不轮询", async () => {
  const page = fakePage(() => true);
  assert.equal(await waitFor(page, "true", "立即", 500), true);
  assert.equal(page.calls, 1);
});

test("条件后来才成立 → 继续轮询直到成立", async () => {
  const page = fakePage((n) => n >= 3);
  assert.equal(await waitFor(page, "later", "稍后", 2000), true);
  assert.ok(page.calls >= 3, `应当轮询到第 3 次，实际 ${page.calls}`);
});

test("求值抛错 → 当作『还没满足』继续轮询，不中断", async () => {
  // 前两次模拟"文档正在换"：document.body 为 null → TypeError
  const page = fakePage((n) => {
    if (n <= 2) throw new TypeError("Cannot read properties of null (reading 'innerText')");
    return true;
  });
  assert.equal(await waitFor(page, "flaky", "抖动后成立", 3000), true);
  assert.ok(page.calls >= 3, "抛错之后必须继续试");
});

test("**一直抛错 → 必须超时失败**，不能因为『有容错』就当成通过", async () => {
  const page = fakePage(() => {
    throw new TypeError("Cannot read properties of null (reading 'innerText')");
  });
  await assert.rejects(
    () => waitFor(page, "always-throws", "一直抛错", 600),
    (error) => {
      assert.match(error.message, /等待超时/);
      assert.match(error.message, /一直抛错/);
      // 关键：超时信息里要带上**最后那次异常**，否则又回到"不说是哪条"
      assert.match(error.message, /最后求值异常/);
      assert.match(error.message, /innerText/);
      return true;
    },
  );
});

test("条件始终为假 → 必须超时失败（容错不能把假变成真）", async () => {
  const page = fakePage(() => false);
  await assert.rejects(
    () => waitFor(page, "never", "永假", 500),
    (error) => {
      assert.match(error.message, /等待超时/);
      assert.match(error.message, /最后结果=false/);
      // 没有异常时**不该**出现"最后求值异常"字样（免得让人以为出过错）
      assert.doesNotMatch(error.message, /最后求值异常/);
      return true;
    },
  );
});

test("抛错之后成功 → 不带异常信息返回（异常只是过程中的噪声）", async () => {
  let threw = false;
  const page = fakePage((n) => {
    if (n === 1) {
      threw = true;
      throw new Error("transient");
    }
    return true;
  });
  assert.equal(await waitFor(page, "recover", "恢复", 2000), true);
  assert.ok(threw, "这一条要真的经历过一次抛错");
});

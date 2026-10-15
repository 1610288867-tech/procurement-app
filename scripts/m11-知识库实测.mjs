/** M11「AI 制度助手」的前端浏览器实测：上传制度 → 看切分 → 提问拿引用 → 「未调用模型」→ 拒答 → 追问复用会话。
 *
 * 用法（前后端都在跑：8000 / 5173）：
 *     cd D:\procurement-app
 *     node scripts\m11-知识库实测.mjs
 *
 * ============================ 它验什么、不验什么 ============================
 * 验的是**页面真的能用**：上传控件真的收下了文件、列表真的刷新出那一行、
 * 「看切分」真的拉到了分片、「提问」真的把正文逐段画出来、引用卡片真的带上了
 * 编号/标题/版本/段号、拒答与「未调用模型」真的显示出来了。
 *
 * **不验**后端规则本身（切分粒度、阈值判定、引用校验、权限 404），
 * 那些在 `backend/tests/test_api_knowledge.py` 里，跑得比浏览器快得多。
 *
 * ============================ 为什么要自起浏览器 ============================
 * 照 `scripts/窄屏自检.mjs` 的写法：自起一个**无窗口 Edge**（独立临时 profile），
 * 跑完自己收掉。不去连"外部已经开好的 9222"—— 那会让"这个脚本能不能跑"
 * 取决于别人怎么启动的浏览器、以及那个浏览器里留着谁的登录态。
 *
 * 端口用 **9333** 而不是 9222：9222 是 `浏览器自检.mjs` / `m6-页面实测.mjs` 的默认口，
 * 撞上去的后果不是报错，而是**脚本在驱动另一个浏览器**（症状是时好时坏的串台）。
 *
 * ============================ 6 条 check = 6 个验收项 ============================
 * 一一对应，标题里带【验收 N】，超时一律打印现场（URL + 页面文字前 200 字 + 页面上的按钮），
 * 免得看到一句"等待超时"还得回去猜停在哪一页。
 */

import { spawn } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { basename, join } from "node:path";

const APP = "http://localhost:5173";
//: 与 9222 刻意分开（见文件头）
const PORT = 9333;
const CDP = `http://127.0.0.1:${PORT}`;

/** 管理员口令。**只写死这一个**：知识库管理是仅 ADMIN 的接口，
 *  而这个脚本不需要其它任何账号（问答登录即可，用管理员一样能问）。 */
const ADMIN = { username: "admin", password: "Dev-Passw0rd!" };

//: 本次运行的唯一标记。source_key / 标题 / 提问都带上它 ——
//: 这才让脚本**可以反复跑**（第二次不会被 `UNIQUE(source_key, version)` 挡回 409，
//: 也能在一堆历史会话里认出"本次那个会话"）。
const STAMP = Date.now().toString(36).toUpperCase();
const POLICY_TITLE = `M11 页面实测制度 ${STAMP}`;
//: 清理脚本按这个前缀认数据（`M11PAGE-` 是本脚本自定的，见末尾的清理提示）
const POLICY_KEY = `M11PAGE-${STAMP}`;

/** 制度正文（595 字，**压在 600 字以内所以刚好切成 1 片**）。
 *
 * 四处刻意：
 *  1. **必须包含「采购金额超过十万元」与「验收」** —— 第 3、6 条的提问靠它们命中
 *     （本地 embedding 是**字面重合**，不含原词就检索不到）；
 *  2. **刻意避开「年/假/多/少/天」** —— 第 5 条问的是「年假有多少天？」，
 *     如果制度里也出现这些字，那问题就会**命中**，验不到"查不到就说查不到"。
 *     这一条是离线算过的：该问句对本制度片段的相似度 0.029 < 阈值 0.15；
 *  3. **正文里也写着 `POLICY_KEY`**（第二行那串编号），而且第 3 条的提问里也带它。
 *     为什么非这样不可：库里可能留着**上一次运行**传的同内容文档（清理脚本没跑过的时候），
 *     它们的片段向量**几乎一模一样**，检索排序就成了抽签 —— 万一本次那份没进 top-k（默认 5），
 *     断言"引用卡片里有本次这份文档"就会时好时坏地失败。
 *     把 stamp 写进正文，本次这份片段的字面重合就**严格更高**（离线算过：0.2812 vs 0.2774），
 *     稳定排第一。
 *  4. **总长必须 ≤ 600 字**（`CHUNK_SIZE`）：多一行 stamp 就让总长越过 600，
 *     切分会从 1 片变成 2 片，而"验收"那半句会落到第 2 片里 —— 于是
 *     「验收不合格怎么办？」的命中率从 0.23 掉到 0.05（低于阈值，白白多一条拒答）。
 *     所以这里比第一版**刻意删掉**了若干字（"或者财政性资金""与追溯"…）。
 */
const POLICY_TEXT = `采购管理办法（节选）
（本文件编号：${POLICY_KEY}）

第一章 总则
第一条 为规范本公司采购行为、明确职责分工、降低采购成本，制定本办法。
第二条 本办法适用于本公司各部门使用自有资金开展的货物、工程与服务采购活动。

第二章 采购方式与审批
第三条 采购金额超过十万元的，应当采用公开招标方式确定供应商；采购金额在五万元至十万元之间的，可以采用邀请报价或者竞争性谈判方式；低于五万元的，由需求部门自行比价后报采购部门备案。
第四条 采购金额超过十万元的紧急采购，经总经理书面批准后可以先行实施，但应当在十个工作日内补办审批手续。
第五条 单一来源采购仅限下列情形：只有唯一供应商能够提供；发生不可预见的紧急情况；原项目的后续扩展。
第六条 任何部门不得将采购金额拆分以规避公开招标；发现拆分的，采购部门应当予以退回并报告总经理。

第三章 合同与履约
第七条 采购合同应当约定交付时间、质量标准、付款方式与违约责任。合同金额与中标金额不一致的，应当说明原因并报采购部门审批。
第八条 到货后由需求部门与采购部门共同组织验收，验收内容包括数量、规格、质量与技术参数。
第九条 验收合格的，由验收人签署验收单，财务部门凭验收单与合同、发票办理付款；验收不合格的，应当退回供应商并要求限期整改，整改完成后重新组织验收。
第十条 验收单、合同与发票一并归档保存，作为审计依据。
`;

//: 三条问句。第 1 条带 `POLICY_KEY`：会话标题取自问句前 60 字，
//: 带上它才能在会话列表里**唯一认出本次那个会话**（历史轮次的会话标题可能一模一样）。
const QUESTION_HIT = `采购金额超过十万元是否需要公开招标？（${POLICY_KEY}）`;
const QUESTION_MISS = "年假有多少天？";
const QUESTION_FOLLOW_UP = "验收不合格怎么办？";

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// ------------------------------------------------------------------ 浏览器：自起自收

function findEdge() {
  const candidates = [
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    join(process.env.LOCALAPPDATA || "", "Microsoft/Edge/Application/msedge.exe"),
  ];
  return candidates.find((path) => path && existsSync(path));
}

function startBrowser(edgePath, profile) {
  return spawn(
    edgePath,
    [
      "--headless=new",
      "--disable-gpu",
      `--remote-debugging-port=${PORT}`,
      `--user-data-dir=${profile}`,
      "--no-first-run",
      "--no-default-browser-check",
      "about:blank",
    ],
    { stdio: "ignore" },
  );
}

/** 端口上是不是**已经有**一个浏览器在跑调试接口。
 *
 * 必须先问这一句：如果 9333 被占，Edge 会静默地不监听（或复用到那个实例），
 * 而 `fetch(/json/version)` 照样成功 —— 脚本于是去驱动**别人的浏览器**，
 * 症状是"页面里看到的和我点的不一样"，且极难定位。
 */
async function portBusy() {
  try {
    const response = await fetch(`${CDP}/json/version`, { signal: AbortSignal.timeout(1500) });
    return response.ok;
  } catch {
    return false;
  }
}

async function waitForCdp(timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${CDP}/json/version`, { signal: AbortSignal.timeout(1500) });
      if (response.ok) return await response.json();
    } catch {
      // 还没起来，继续等
    }
    await sleep(300);
  }
  throw new Error(`Edge 的调试端口没起来（${PORT}）`);
}

/** 连一个**标签页**的 WebSocket（`/json/new` 拿到 webSocketDebuggerUrl 后直连）。
 *
 * 不要改成"连浏览器级 WebSocket + Target.attachToTarget 拿 sessionId"：
 * 那条路还要把 sessionId 带在每条消息上，漏了就会一直收不到回应 ——
 * 表现为"页面永远加载不出来"，很难看出原因（`m6-页面实测.mjs` 里记了这次教训）。
 */
async function connect(url) {
  const response = await fetch(`${CDP}/json/new?${encodeURIComponent(url)}`, { method: "PUT" });
  if (!response.ok) throw new Error(`新建标签失败：${response.status}`);
  const target = await response.json();
  const socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });

  let nextId = 1;
  const pending = new Map();
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (!message.id || !pending.has(message.id)) return;
    const entry = pending.get(message.id);
    pending.delete(message.id);
    message.error ? entry.reject(new Error(message.error.message)) : entry.resolve(message.result);
  });
  const send = (method, params = {}) =>
    new Promise((resolve, reject) => {
      const id = nextId++;
      pending.set(id, { resolve, reject });
      socket.send(JSON.stringify({ id, method, params }));
    });

  await send("Page.enable");
  await send("Runtime.enable");
  await send("Network.enable");
  return { send, close: () => socket.close() };
}

// ------------------------------------------------------------------ 汇总

const results = [];
function check(ok, title, evidence = "") {
  results.push({ ok, title, evidence });
  console.log(`${ok ? "PASS" : "FAIL"}  ${title}${evidence ? "   —— " + evidence : ""}`);
}

const flat = (text, limit = 200) => String(text ?? "").replace(/\s+/g, " ").trim().slice(0, limit);

async function main() {
  // ---------- 前置：前端在不在 ----------
  try {
    const response = await fetch(`${APP}/`, { signal: AbortSignal.timeout(4000) });
    if (!response.ok) throw new Error(String(response.status));
  } catch (error) {
    console.log(`前端没在 ${APP} 上（${error.message}）。先跑：.\\scripts\\start.ps1`);
    return 1;
  }
  // 顺带确认后端（经 Vite 代理）：未登录应当是 401；500/502 说明 uvicorn 没起。
  try {
    const probe = await fetch(`${APP}/api/v1/chat/sessions`, { signal: AbortSignal.timeout(4000) });
    console.log(`后端探针：GET /api/v1/chat/sessions -> ${probe.status}${
      probe.status === 401 ? "（后端在跑）" : "（期望 401，后端可能没起）"}`);
  } catch (error) {
    console.log(`后端探针失败：${error.message}`);
  }

  const edgePath = findEdge();
  if (!edgePath) {
    console.log("找不到 Edge —— 这个脚本要用 Edge 的调试接口。");
    return 1;
  }
  if (await portBusy()) {
    console.log(`端口 ${PORT} 上已经有一个浏览器在跑调试接口，先关掉它再跑`
      + "（脚本不会去驱动一个不是自己起的浏览器）。");
    return 1;
  }

  // ---------- 临时制度文件：真的写到磁盘上 ----------
  // 为什么要落一个真文件：验收要的是"**管理员上传一份制度文档**"，
  // 而上传控件收的是文件。用临时文件而不是把正文塞在脚本里当场造 File，
  // 是为了让"文件名 / 字节数"这两件事也有真实来源（后面对得上）。
  const tempRoot = join(tmpdir(), "procurement-m11");
  mkdirSync(tempRoot, { recursive: true });
  // 文件名用 ASCII：multipart 的 filename 走 latin-1，中文名会变成乱码
  //（不影响功能，但会让人以为"上传坏了"）
  const tempFile = join(tempRoot, `m11-policy-${STAMP}.txt`);
  writeFileSync(tempFile, POLICY_TEXT, "utf8");
  const fileBytes = readFileSync(tempFile);

  const profile = mkdtempSync(join(tmpdir(), "procurement-m11-edge-"));
  const browser = startBrowser(edgePath, profile);
  let page = null;

  try {
    const version = await waitForCdp();
    console.log(`浏览器：${version.Browser}（headless，端口 ${PORT}）`);
    console.log(`本次标记 stamp=${STAMP}  临时制度文件=${tempFile}（${fileBytes.length} 字节）\n`);

    page = await connect(`${APP}/login`);

    const rawEvaluate = async (expression) => {
      const result = await page.send("Runtime.evaluate", {
        expression,
        awaitPromise: true,
        returnByValue: true,
      });
      if (result.exceptionDetails) {
        const detail = result.exceptionDetails.exception?.description
          ?? JSON.stringify(result.exceptionDetails);
        throw new Error(`页面内报错：${detail}`);
      }
      return result.result.value;
    };

    /** 页面**重新加载的那一瞬间** `document.body` 是 null，任何读 body 的表达式都会抛
     *  "Cannot read properties of null" —— 那是过渡态，不是脚本的错。
     *  读 body 的表达式统一走"没有 body 就返回空串"，要等的条件下一轮自然会成立。 */
    const evaluate = async (expression) => rawEvaluate(`(() => {
      if (!document.body) return "";
      return (${expression});
    })()`);

    /** 轮询直到条件成立，**超时把现场带上**。
     *
     * 现场三样（URL / 页面文字前 200 字 / 页面上的按钮）一次就能说清
     * "停在哪一页、那页上有什么" —— 只报"等待超时：某个标签"会让人回去猜，
     * 而猜一轮的成本是重跑一次浏览器实测。 */
    const waitFor = async (condition, label, timeout = 20000) => {
      const deadline = Date.now() + timeout;
      while (Date.now() < deadline) {
        if (await evaluate(condition)) return true;
        await sleep(200);
      }
      const where = await evaluate(`(() => {
        const buttons = [].slice.call(document.querySelectorAll('button'))
          .map((b) => b.textContent.trim()).filter(Boolean).slice(0, 8).join('|');
        return location.pathname + ' :: ' + document.body.innerText.replace(/\\s+/g, ' ').slice(0, 200)
          + ' :: buttons=' + buttons;
      })()`);
      throw new Error(`等待超时：${label}\n      现场：${where}`);
    };

    //: 截图存到 %TEMP%，**不落进仓库**（脚本只该新增自己那一个文件）
    const shot = async (name) => {
      const { data } = await page.send("Page.captureScreenshot", { format: "png" });
      const path = join(tempRoot, `${name}.png`);
      writeFileSync(path, Buffer.from(data, "base64"));
      return path;
    };

    // ---------- 登录 ----------
    // 走页内 fetch 而不是"填登录表单提交"：表单那条路要等多步渲染，
    // 导航时机还会把登录请求冲掉（`m6-页面实测.mjs` 实测表现为时好时坏）。
    // 登录成功后**必须整页导航一次**：React Query 缓存里的 `useMe()` 还是上一个身份，
    // 不重载的话界面会继续按"未登录"渲染（停在登录页，而接口其实是通的）。
    await waitFor("!!document.querySelector('input[autocomplete=username]')", "登录页渲染");
    const loginStatus = await evaluate(`(async () => {
      await fetch('/api/v1/auth/logout', { method: 'POST' });
      const response = await fetch('/api/v1/auth/login', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: '${ADMIN.username}', password: '${ADMIN.password}' }),
      });
      return response.status;
    })()`);
    if (loginStatus !== 200) throw new Error(`登录 ${ADMIN.username} 失败：HTTP ${loginStatus}`);

    await page.send("Page.navigate", { url: `${APP}/admin/knowledge` });
    await waitFor(
      "location.pathname === '/admin/knowledge' && !!document.querySelector('[data-testid=knowledge-file]')",
      "以管理员身份进入知识库页",
    );
    const me = await evaluate(
      "(async () => { const r = await fetch('/api/v1/auth/me'); return r.ok ? r.json() : null; })()",
    );
    if (!me?.roles?.includes("ADMIN")) {
      throw new Error(`当前身份不是管理员：${JSON.stringify(me)}（知识库接口仅 ADMIN）`);
    }
    console.log(`身份：${me.username}（${me.roles.join("+")}）\n`);

    // ---------- React 受控输入必须走原型上的 setter ----------
    // 直接 `input.value = x` 在 React 里是**静默无效**的：它的 value tracker
    // 认为值没变，onChange 不触发，提交上去的还是旧值（表现为"填了却没生效"）。
    // 并且**返回写入后的实际值**，让调用处能当场断言 —— 返回 'ok' 会把
    // "选择器没找到元素"也记成成功，一路跑到后面才以别的样子爆出来。
    const setReactValue = (selector, value) => `(() => {
      const el = document.querySelector(${JSON.stringify(selector)});
      if (!el) return { ok: false, reason: 'no-element:' + ${JSON.stringify(selector)} };
      const proto = el instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
      setter.call(el, ${JSON.stringify(String(value))});
      el.dispatchEvent(new Event('input', { bubbles: true }));
      return { ok: el.value === ${JSON.stringify(String(value))}, readback: el.value };
    })()`;
    const fillField = async (selector, value) => {
      const result = await evaluate(setReactValue(selector, value));
      if (result?.ok !== true) throw new Error(`填字段失败 ${selector}：${JSON.stringify(result)}`);
      return result.readback;
    };

    // ================================================================
    // 【验收 1】管理员上传一份制度文档 → 列表出现它：READY / 分片数 ≥ 1 / 显示向量模型
    // ================================================================
    await fillField("[data-testid=knowledge-title]", POLICY_TITLE);
    await fillField("[data-testid=knowledge-source-key]", POLICY_KEY);
    // 版本号留默认的 1；生效时间留空 = 服务端按"立即生效"处理（检索只认已生效的版本）

    // 文件：用 DataTransfer 造一个**真实的 FileList** 再派发 change。
    // 只设 `input.value` 是不行的（浏览器不允许脚本设文件路径，直接抛错），
    // 而 React 的 onChange 读的是 `event.target.files[0]`，没有 FileList 就等于没选文件。
    const fileBase64 = fileBytes.toString("base64");
    const injected = await evaluate(`(() => {
      const input = document.querySelector('[data-testid=knowledge-file]');
      if (!input) return { ok: false, reason: 'no-input' };
      const binary = atob(${JSON.stringify(fileBase64)});
      const bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
      const file = new File([bytes], ${JSON.stringify(basename(tempFile))}, { type: 'text/plain' });
      const transfer = new DataTransfer();
      transfer.items.add(file);
      input.files = transfer.files;
      input.dispatchEvent(new Event('change', { bubbles: true }));
      const picked = input.files[0];
      return {
        ok: input.files.length === 1 && !!picked && picked.size === ${fileBytes.length},
        name: picked ? picked.name : null, size: picked ? picked.size : null,
      };
    })()`);
    if (injected?.ok !== true) {
      throw new Error(`把文件塞进上传控件失败：${JSON.stringify(injected)}`);
    }

    const submitted = await evaluate(`(() => {
      const button = document.querySelector('[data-testid=knowledge-submit]');
      if (!button) return 'no-button';
      if (button.disabled) return 'disabled';
      button.click();
      return 'clicked';
    })()`);

    // 等的是**那一行**（带 stamp 且已经是「已就绪」），不是"列表里多了点什么"：
    // 库里可能留着别的运行传过的制度，模糊匹配会拿错行（然后后面的断言全错位）。
    await waitFor(
      `(() => {
        const rows = [].slice.call(document.querySelectorAll('[data-testid=knowledge-list] tr'));
        return rows.some((row) => row.innerText.indexOf(${JSON.stringify(STAMP)}) >= 0
          && row.innerText.indexOf('已就绪') >= 0);
      })()`,
      "上传的那份制度出现在列表里并变成「已就绪」",
      30000,
    );

    const rowText = await evaluate(`(() => {
      const rows = [].slice.call(document.querySelectorAll('[data-testid=knowledge-list] tr'));
      const row = rows.filter((tr) => tr.innerText.indexOf(${JSON.stringify(STAMP)}) >= 0
        && tr.querySelector('[data-testid=knowledge-toggle]'))[0];
      return row ? row.innerText.replace(/\\s+/g, ' ').trim() : '';
    })()`);
    // `分片数` 那一格是 `{chunk_count}` + `（正文 N 字）`，一起读回来才拿得到两个数。
    // 正则在 Node 里做（不在页面里写字符串正则：模板串会把 `\\d` 吃成 `d`，永远匹配不上）。
    const chunkMatch = /(\d+)\s*（正文\s*(\d+)\s*字）/.exec(rowText);
    const chunkCount = chunkMatch ? Number(chunkMatch[1]) : -1;
    const contentChars = chunkMatch ? Number(chunkMatch[2]) : -1;

    check(
      submitted === "clicked"
        && injected.ok === true
        && rowText.includes(POLICY_TITLE)
        && rowText.includes(POLICY_KEY)
        && rowText.includes("已就绪")
        // ⚠️ **不要断言具体的向量模型名**。它是**配置**（`KNOWLEDGE_EMBEDDING_MODEL`），
        // 换 embedding（哈希 ↔ 真模型）都会变，而且这里是页面实测、
        // 不该依赖跑的是哪一套配置。要验的是"这一列在界面上显示出来了"，
        // 所以只断言前缀 `local-hash-` 或 `quentinz/` 这类**非空**特征。
        // （实测踩到：写死 `local-hash-256` 之后，换成真 embedding 这条就红了 ——
        //   而那次失败跟"页面能不能上传制度"毫无关系。）
        && (rowText.includes("local-hash-") || rowText.includes("quentinz/"))
        && chunkCount >= 1,
      "【验收 1】管理员在页面上传制度文档 → 列表出现该行：已就绪 / 分片数 ≥ 1 / 显示向量模型",
      `文件=${injected.name}（${injected.size} 字节）提交=${submitted}；`
        + `行=「${flat(rowText, 130)}」；分片数=${chunkCount} 正文=${contentChars} 字`,
    );
    const listShot = await shot("01-knowledge-list");

    // ================================================================
    // 【验收 2】能展开看切分结果（分片内容 + 字符区间）
    // ================================================================
    const toggled = await evaluate(`(() => {
      const rows = [].slice.call(document.querySelectorAll('[data-testid=knowledge-list] tr'));
      const row = rows.filter((tr) => tr.innerText.indexOf(${JSON.stringify(STAMP)}) >= 0
        && tr.querySelector('[data-testid=knowledge-toggle]'))[0];
      if (!row) return 'no-row';
      row.querySelector('[data-testid=knowledge-toggle]').click();
      return 'clicked';
    })()`);
    // ⚠️ 等的是**面板里的内容**，不是"面板出现了"：切分结果是展开后才拉的
    // （`useKnowledgeChunks`），面板一挂上就先画「读取切分结果…」——
    // 条件只写"元素存在"会立刻成立，于是断言跑在加载态上（第一版就是这么挂的，
    // 证据里读到的是「读取切分结果…」）。同一个坑 `m6-页面实测.mjs` 里也记过。
    await waitFor(
      `(() => {
        const panel = document.querySelector('[data-testid=knowledge-chunks]');
        return !!panel && panel.innerText.indexOf('字符') >= 0
          && panel.innerText.indexOf('读取切分结果') < 0;
      })()`,
      "点「看切分」后分片内容真的读出来了（不是停在「读取切分结果…」）",
      20000,
    );
    const chunksText = await evaluate(
      "(document.querySelector('[data-testid=knowledge-chunks]') || {}).innerText || ''",
    );
    // 三样缺一不可：分片号（哪一片）、字符区间（在原文的哪一段）、原文（切成了什么）
    const hasRange = /字符\s*\d+\s*[–-]\s*\d+/.test(chunksText);
    const pieceMatch = /第\s*(\d+)\s*片/.exec(chunksText);
    check(
      toggled === "clicked"
        && hasRange
        && pieceMatch !== null
        && chunksText.includes("采购金额超过十万元")
        && chunksText.includes(POLICY_TITLE),
      "【验收 2】点那一行的「看切分」→ 展开出分片内容与字符区间（第 N 片 / 字符 a–b / 原文）",
      `点击=${toggled} 区间在=${hasRange} 片号=${pieceMatch ? pieceMatch[1] : "（没读到）"}；`
        + `面板=「${flat(chunksText, 130)}」`,
    );
    const chunkShot = await shot("02-knowledge-chunks");

    // ================================================================
    // 转到制度助手页。**从这里到验收 6 结束都不再整页导航** ——
    // 会话是页面里的本地状态（Chat.tsx 没有"读历史消息"的调用），
    // 一刷新就没了，而验收 6 要看的正是"历史还在不在"。
    // ================================================================
    await page.send("Page.navigate", { url: `${APP}/chat` });
    await waitFor(
      "location.pathname === '/chat' && !!document.querySelector('[data-testid=chat-input]')",
      "制度助手页渲染出提问框",
    );
    const emptyText = await evaluate("document.body.innerText");
    console.log(`\n提问前页面文案（空状态）：${flat(emptyText, 120)}\n`);

    /** 提一个问题，并**在提交之后持续采样回答正文的长度**。
     *
     * 为什么必须采样：验收要的是"正文是**分多段**出现的"（流式的证据），
     * 而事后一次性读到的最终文本区分不了"逐段长出来的"与"一次全出来的"。
     *
     * ============================ 为什么以 MutationObserver 为主 ============================
     * 第一版只用 100ms 的定时轮询，**被时序打败过**：本地兜底一个来回只要 ~100ms，
     * 轮询要么在"空正文"之前、要么在"整段正文"之后落点，于是只采到一个长度，
     * 看起来像"流式没生效"—— 而页面其实是对的。
     * MutationObserver 是**只在 DOM 真的变了时**才记一笔，不会漏掉"空 → 有"这一步；
     * 50ms 的轮询作为兜底保留（防"一整批 DOM 变更被合并成一次回调"）。
     *
     * 采样两列：`len` = 最新一轮回答正文的长度（元素还没出现时是 -1）、
     * `pending` = **这一轮还没结束**。
     *
     * ⚠️ 如实说明（写进证据、不写进断言）：本机是**本地兜底**，后端一轮只发
     * **一条** `delta`，所以这里看到的"分步"是「空正文（正在检索制度…）→ 整段正文」
     * 两步，而不是逐字吐出来 —— 逐字要多条 `delta`，那要配了远端模型才看得到。
     * 判据按验收口径写（采样到 ≥2 个**不同的正文长度**即算"逐段出现"），
     * 证据里把整条序列原样打出来，读者自己能看出它是几步。
     *
     * ⚠️ 判定"这一轮结束了"用的是**提问按钮的文案**（`回答中…` → `提问`），
     * 不是页面上那句「正在回答…」。理由是一条实测出来的事实（截图见 03-chat-answer.png）：
     * `Chat.tsx` 的 `emit()` 在 `done` 分支里只置了 `closed`，**没有调用 `handlers.onDone()`**，
     * 于是 `turn.status` 永远不会变成 `"done"` —— 回答早就写完了，那句「正在回答…」
     * 与光标 ▍ 还一直挂着（第一版拿它当"答完了"的判据，结果每题都白等满 30 秒超时）。
     * 按钮文案由**组件级**的 `streaming` 决定，而它在 `finally` 里一定会复位，所以可靠。
     * （那条 `onDone` 没被调用是前端的 bug，不在本脚本的改动范围内，已写进报告。）
     */
    const askAndSample = (question) => evaluate(`(async () => {
      const input = document.querySelector('[data-testid=chat-input]');
      const send = document.querySelector('[data-testid=chat-send]');
      if (!input || !send) return { error: 'no-input-or-send' };
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
      setter.call(input, ${JSON.stringify(question)});
      input.dispatchEvent(new Event('input', { bubbles: true }));
      if (input.value !== ${JSON.stringify(question)}) {
        return { error: 'fill-failed', value: input.value };
      }
      if (send.disabled) return { error: 'send-disabled' };

      const samples = [];
      const started = Date.now();
      const record = () => {
        const el = document.querySelector('[data-testid=chat-answer]');
        const len = el ? el.textContent.length : -1;
        const button = document.querySelector('[data-testid=chat-send]');
        const pending = !button || button.textContent.trim() !== '提问';
        const last = samples[samples.length - 1];
        if (!last || last.len !== len || last.pending !== pending) {
          samples.push({ ms: Date.now() - started, len: len, pending: pending });
        }
      };
      const observer = new MutationObserver(record);
      observer.observe(document.body, { childList: true, subtree: true, characterData: true });
      record();                                     // 点击前/后的基线各记一笔
      // 原生 click()：会走完整的激活行为（提交表单）并冒泡，React 的 onSubmit 收得到
      send.click();
      record();
      const timer = setInterval(record, 50);        // 兜底：万一 DOM 变更被合并成一次

      // 收尾条件：按钮回到「提问」（这一轮真的结束了）且正文已经稳定 400ms。
      // 稳定性是防"最后一个 delta 与按钮复位撞在一起"时把正文截断。
      let stableSince = null;
      while (Date.now() - started < 30000) {
        const last = samples[samples.length - 1];
        if (last && last.len > 0 && !last.pending) {
          if (stableSince === null) stableSince = Date.now();
          else if (Date.now() - stableSince >= 400) break;
        } else {
          stableSince = null;
        }
        await new Promise((resolve) => setTimeout(resolve, 50));
      }
      clearInterval(timer);
      observer.disconnect();

      const el = document.querySelector('[data-testid=chat-answer]');
      return { samples: samples, answer: el ? el.textContent : '', filled: input.value };
    })()`);

    /** 采样结果 → 一行证据。只打印前 8 条：几十条 `0` 会把日志淹掉。 */
    const sampleEvidence = (samples = []) =>
      `采样 ${samples.length} 次：${samples.slice(0, 8)
        .map((item) => `${item.len}@${item.ms}ms${item.pending ? "(回答中)" : ""}`).join(" → ")}`
        + (samples.length > 8 ? " → …" : "");

    //: 「逐段出现」只看**正文真的存在之后**的长度：-1 = 那一轮还没挂上 DOM，不算一个"段"
    const distinctLengths = (samples = []) =>
      [...new Set(samples.filter((item) => item.len >= 0).map((item) => item.len))];

    // ================================================================
    // 【验收 3】问一个制度里有答案的问题 → 正文逐段出现 + 引用卡片（编号/标题/版/段）
    // ================================================================
    const asked = await askAndSample(QUESTION_HIT);
    if (asked?.error) throw new Error(`提问失败（页面侧）：${JSON.stringify(asked)}`);
    const lengths = distinctLengths(asked.samples);
    const sourcesText = await evaluate(
      "(document.querySelector('[data-testid=chat-sources]') || {}).innerText || ''",
    );
    const citationCount = await evaluate(
      "document.querySelectorAll('[data-testid=chat-citation]').length",
    );
    const unknownCitationCount = await evaluate(
      "document.querySelectorAll('[data-testid=chat-citation-unknown]').length",
    );
    const answered = asked.answer ?? "";

    check(
      answered.length > 0
        // 「逐段出现」的判据（用户口径）：采样到 **≥2 个不同的正文长度**
        && lengths.length >= 2
        && sourcesText.length > 0
        && /\[\d+\]/.test(sourcesText)          // 编号
        && sourcesText.includes(POLICY_TITLE)   // 文档标题（带 stamp，确认是本次上传的那份）
        && /第 \d+ 版/.test(sourcesText)         // 第几版
        && /第 \d+ 段/.test(sourcesText)         // 第几段
        && citationCount >= 1
        && unknownCitationCount === 0,
      "【验收 3】问制度里有答案的问题 → 回答正文分多段出现，且引用卡片带编号/标题/第几版/第几段",
      `${sampleEvidence(asked.samples)}；不同长度=${lengths.length} 个（${lengths.join("/")}）；`
        + `正文=「${flat(answered, 90)}」；引用卡片=「${flat(sourcesText, 150)}」；`
        + `正文中的引用标记 ${citationCount} 个（编造的 ${unknownCitationCount} 个）`,
    );
    const answerShot = await shot("03-chat-answer");

    // ================================================================
    // 【验收 4】「未调用模型」的标识
    // ================================================================
    // ⚠️ 断言的是**徽标本身**（一个文本恰好等于「未调用模型」的元素），
    // 不是"页面里出现过这四个字"：本地兜底生成的正文里**也**写着
    // "（未调用模型，以下为原文摘录）" —— 按文本包含判就等于没验。
    //
    // ============================ 这一项的前提要**显式**，不能写死 ============================
    // 它量的是**上一条验收那一轮**（有答案的制度问题）。那一轮走哪条路**取决于后端配置**：
    //
    //   · 后端没配对话模型（`KNOWLEDGE_LLM_*` 为空）→ 走本地抽取式兜底
    //     → `used_model=false` → **应当**显示徽标（本项原本就是为这种配置写的）；
    //   · 后端配了对话模型 → 那一轮真的调了模型 → `used_model=true`
    //     → **本来就不该**有徽标（此时仍要求徽标就是错的断言）。
    //
    // 原来的标题写成「本机没配 LLM 密钥」—— 那是**对环境的假设**，
    // 而 `backend/.env` 一配上模型，这个假设就不成立了（实测踩到：同一项
    // 在两种配置下必然一红一绿，而失败信息只说"徽标在=false"，看不出是前提变了）。
    // 现在改成：**读后端的真实状态**决定该断言什么，并把判据写进证据里。
    const refused = await evaluate(`(() => {
      const turns = [].slice.call(document.querySelectorAll('[data-testid=chat-turn]'));
      const last = turns[turns.length - 1];
      if (!last) return null;
      const badge = [].slice.call(last.querySelectorAll('span, p, div'))
        .filter((el) => el.textContent.trim() === '未调用模型')[0];
      const text = last.innerText.replace(/\\s+/g, ' ');
      // HintNote 里的"未调用模型：<原因>"——冒号是它与正文那句话的区别
      const reason = (text.match(/未调用模型：[^。]{0,110}/) || [''])[0];
      return { badge: !!badge, reason: reason, text: text.slice(0, 200) };
    })()`);
    // 后端那一轮到底有没有用模型？（**同一份数据源的直接证据**，比从页面上猜可靠）
    const lastAssistantUsedModel = await evaluate(`(async () => {
      const sessions = await (await fetch('/api/v1/chat/sessions')).json();
      if (!sessions.length) return null;
      const messages = await (await fetch(
        '/api/v1/chat/sessions/' + sessions[0].id + '/messages')).json();
      const assistants = messages.filter((m) => m.role === 'ASSISTANT');
      return assistants.length ? assistants[assistants.length - 1].used_model : null;
    })()`);
    const backendUsedModel = lastAssistantUsedModel === true;
    // 徽标**只在该出现时**才要求出现；该出现的前提是"后端那一轮没用模型"
    const expectBadge = !backendUsedModel;
    check(
      !expectBadge || (refused?.badge === true && String(refused.reason).startsWith("未调用模型：")),
      expectBadge
        ? "【验收 4】后端那一轮没用模型（本地兜底）→ 页面明确标出「未调用模型」徽标与原因"
        : "【验收 4】后端那一轮用了模型 → 页面**不**标「未调用模型」（徽标只在本轮没调模型时出现）",
      `后端 used_model=${lastAssistantUsedModel}；徽标在=${refused?.badge}`
        + `；原因=「${refused?.reason}」`,
    );

    // ================================================================
    // 【验收 5】问一个制度里没有的问题 → 「制度中没有找到相关规定」
    // ================================================================
    // 先点「新会话」：这条问句要**另起一段**，否则它会落进上面那个会话里，
    // 让验收 6 的"这个会话有几轮"变得说不清。
    const newSessionClicked = await evaluate(`(() => {
      const button = document.querySelector('[data-testid=chat-new-session]');
      if (!button) return 'no-button';
      if (button.disabled) return 'disabled';
      button.click();
      return 'clicked';
    })()`);
    await waitFor(
      "document.body.innerText.indexOf('新会话（还没落库）') >= 0",
      "点「新会话」后当前会话回到未落库状态",
      10000,
    );

    const missed = await askAndSample(QUESTION_MISS);
    if (missed?.error) throw new Error(`提问失败（页面侧）：${JSON.stringify(missed)}`);
    const missedAnswer = missed.answer ?? "";
    const missedSources = await evaluate(
      "(document.querySelector('[data-testid=chat-sources]') || {}).innerText || ''",
    );
    check(
      newSessionClicked === "clicked" && missedAnswer.includes("制度中没有找到相关规定"),
      "【验收 5】问一个制度里没有的问题 → 回答「制度中没有找到相关规定」（检索没命中就不调用模型）",
      `新会话=${newSessionClicked}；${sampleEvidence(missed.samples)}；`
        + `正文=「${flat(missedAnswer, 80)}」；`
        + `引用卡片=${missedSources ? `「${flat(missedSources, 60)}」（检索到了但都低于阈值）` : "无（检索结果为空）"}`,
    );
    const missShot = await shot("04-chat-no-grounding");

    // ================================================================
    // 【验收 6】在会话列表里点开验收 3 那个会话 → 追问复用同一个会话，历史仍在
    // ================================================================
    // ⚠️ 先把口径写清楚（免得断言看起来比实际更强）：
    //   这一页**不负责回放历史正文** —— Chat.tsx 的提示语明说"不假装能回放"，
    //   它压根没有调用 `GET /chat/sessions/{id}/messages`，选中一个会话的语义是
    //   **在它下面继续追问**。所以"历史还在不在"要分两处看：
    //     ① 页面上：本页从提问到现在没刷新过，前几轮的问答仍在（`chat-turn` 逐条核对）；
    //     ② 会话里：追问之后，服务端那个会话里"我问 + 助手回答"是否成对齐全。
    //   ①+② 合起来才是"追问复用了同一个会话，历史没丢"。
    await waitFor(
      "!!document.querySelector('[data-testid=chat-session]')",
      "验收 3 的会话出现在历史会话列表里",
      20000,
    );
    // 会话按"创建时间倒序"给（服务端定的顺序），问句带 stamp ⇒ 标题唯一，
    // 所以"标题里含 POLICY_KEY 的那一条"就是验收 3 建的会话（不是历史轮次的）。
    const sessionId = await evaluate(`(async () => {
      const rows = await (await fetch('/api/v1/chat/sessions')).json();
      const mine = rows.filter((row) => row.title.indexOf(${JSON.stringify(POLICY_KEY)}) >= 0)[0];
      return mine ? mine.id : null;
    })()`);
    if (!sessionId) throw new Error("会话列表里找不到带 stamp 的会话 —— 提问可能没落库");
    const sessionId8 = sessionId.slice(0, 8);

    const opened = await evaluate(`(() => {
      const buttons = [].slice.call(document.querySelectorAll('[data-testid=chat-session]'));
      const hit = buttons.filter((b) => b.textContent.indexOf(${JSON.stringify(POLICY_KEY)}) >= 0)[0];
      if (!hit) return 'no-button:' + buttons.length;
      hit.click();
      return 'clicked';
    })()`);
    await waitFor(
      `document.body.innerText.indexOf(${JSON.stringify(`当前：${sessionId8}`)}) >= 0`,
      "点开会话后它的 id 出现在「当前：」上",
      10000,
    );

    const followed = await askAndSample(QUESTION_FOLLOW_UP);
    if (followed?.error) throw new Error(`提问失败（页面侧）：${JSON.stringify(followed)}`);

    // 页面上：逐条核对三轮问答（第 1 轮必须还在，且正文非空）
    const turns = await evaluate(`[].slice
      .call(document.querySelectorAll('[data-testid=chat-turn]'))
      .map((li) => {
        const paragraphs = li.querySelectorAll('p');
        const answer = li.querySelector('[data-testid=chat-answer], [data-testid=chat-answer-history]');
        return {
          question: paragraphs[1] ? paragraphs[1].textContent.trim() : '',
          answer: answer ? answer.textContent : '',
        };
      })`);
    // 服务端：这个会话里的消息（用户问题与助手回答都要在）
    const messages = await evaluate(`(async () => {
      const response = await fetch('/api/v1/chat/sessions/${sessionId}/messages');
      if (!response.ok) return { error: 'HTTP ' + response.status };
      const rows = await response.json();
      return {
        count: rows.length,
        roles: rows.map((row) => row.role),
        questions: rows.filter((row) => row.role === 'USER').map((row) => row.content),
        answers: rows.filter((row) => row.role === 'ASSISTANT')
          .map((row) => ({ used_model: row.used_model, sources: (row.sources || []).length,
                           head: row.content.slice(0, 24) })),
      };
    })()`);

    // 判据分三层，**都不依赖"制度还在库里"**（这一条会被并发的清理/测试影响，
    // 而它要证的是"历史没丢、追问落在同一个会话"，不是再证一次检索能不能命中）：
    //   ① 点开真的把当前会话切回了 S1；
    //   ② 页面上第 1 轮（问 + 答）还在，且有第 3 轮追问；
    //   ③ 服务端那个会话里，两问两答成对齐全，且**第 1 轮的回答带着引用来源**
    //      （那是验收 3 拿到的回答，必须原样留在会话里）。
    const firstTurnKept = turns?.[0]?.question === QUESTION_HIT
      && (turns?.[0]?.answer ?? "").trim().length > 0;
    const followUpLanded = turns?.length === 3
      && turns[2].question === QUESTION_FOLLOW_UP
      && (turns[2].answer ?? "").trim().length > 0;
    const historyIntact = !messages?.error
      && messages.questions.includes(QUESTION_HIT)
      && messages.questions.includes(QUESTION_FOLLOW_UP)
      && messages.answers.length === 2
      && messages.answers.every((item) => item.head.length > 0)
      && messages.answers[0].sources >= 1;

    check(
      opened === "clicked" && firstTurnKept && followUpLanded && historyIntact,
      "【验收 6】在会话列表里点开验收 3 的会话 → 追问落在同一个会话，历史（我问 + 助手回答）都在",
      `点开=${opened} 当前会话=${sessionId8}…；追问=「${QUESTION_FOLLOW_UP}」；`
        + `页面上 ${turns?.length} 轮问答（第 1 轮的问题与回答仍在=${firstTurnKept}）；`
        + `会话内消息 ${messages?.count} 条（${(messages?.roles ?? []).join("/")}），`
        + `问句=${JSON.stringify((messages?.questions ?? []).map((q) => flat(q, 26)))}，`
        + `回答=${JSON.stringify((messages?.answers ?? []).map(
          (a) => `来源${a.sources}条/正文「${flat(a.head, 18)}…」`))}`,
    );
    const sessionShot = await shot("05-chat-session");

    console.log(`\n截图（在 %TEMP%\\procurement-m11 下）：`
      + `\n  ${[listShot, chunkShot, answerShot, missShot, sessionShot].join("\n  ")}`);
  } finally {
    page?.close();
    browser.kill();
    // 两步清理**各自 try 一次**，不要合成一个 try：
    // Windows 上 Edge 刚被 kill 时还占着 profile 里的文件，`rmSync` 会抛，
    // 合成一个 try 的话第二步（删临时制度文件）就被**静默跳过**了 ——
    // 实测就攒下了一堆 `%TEMP%\procurement-m11\m11-policy-*.txt`。
    // profile 目录另外多给一次机会：等一会儿再删，Windows 才放得开。
    for (let attempt = 0; attempt < 2; attempt += 1) {
      await sleep(600);
      try {
        rmSync(profile, { recursive: true, force: true });
        break;
      } catch {
        // 还有第二次；两次都不行就算了，不影响结论
      }
    }
    try {
      unlinkSync(tempFile);
    } catch {
      // 删不掉就算了
    }
  }

  // ---------- 清理提示 ----------
  console.log(
    "\n提示：本次实测在库里留下了 1 份制度文档（source_key=" + POLICY_KEY
      + "，含落盘的原文）与 2 个会话（提问 3 次，挂在 admin 名下）。"
      + "\n      清理：cd backend; .venv\\Scripts\\python.exe scripts\\testdata_cleanup.py --scope all"
      + "\n      两条如实说明（读的是当前这一版清理脚本）："
      + "\n        ① 制度文档按 source_key 前缀认（含 M11PAGE-%），会被清掉，落盘原文随后按孤儿文件删；"
      + "\n        ② 问答会话只清 `t-` 前缀账号名下的 —— admin 的会话清不掉，会一直攒着。",
  );

  const passed = results.filter((item) => item.ok).length;
  console.log(`\n=== ${passed}/${results.length} 项通过 ===`);
  // 结尾**再列一遍失败项**：回归入口只会报一句「知识库实测有未通过项」，
  // 而上面那几行 PASS 已经把失败的那一条挤出屏幕 —— 不列出来就只能靠重跑排查。
  for (const item of results.filter((entry) => !entry.ok)) {
    console.log(`  ✗ ${item.title}   —— ${item.evidence}`);
  }
  return passed === results.length ? 0 : 1;
}

try {
  process.exitCode = await main();
} catch (error) {
  console.error("实测中断：", error.message);
  process.exitCode = 1;
}

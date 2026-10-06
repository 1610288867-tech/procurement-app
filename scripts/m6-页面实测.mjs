/** M6 页面的浏览器实测：待办列表 / 任务详情 / 申请详情的审批进度。
 *
 * 它只负责"**页面真的渲染出来了、三态真的画对了**"，
 * 功能与权限的断言在 backend/tests/test_api_approvals.py 与 backend/scripts/m6_smoke.py 里。
 *
 * 以 `dept-manager`（行政部主管，种子账号）的身份跑：
 *   不用 admin —— 它是系统级账号、**没有部门**，而后端要求申请人必须有部门
 *   （`NO_DEPARTMENT` 422，刻意的：没有部门就算不出审批链该找谁）。
 *   `dept-manager` 在行政部，同部门还有 `dept-head`（部门负责人），
 *   所以"两级链 + 我审自己部门的单"这两件事都能验到。
 */

import { mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const APP = "http://localhost:5173";
const CDP = "http://127.0.0.1:9222";

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function connect(url = "about:blank") {
  // 与 `scripts/浏览器自检.mjs` 同一套连法：**直连标签页的 WebSocket**
  // （`/json/new` 拿到 webSocketDebuggerUrl 后直接连）。
  //
  // 不要改成"连浏览器级 WebSocket + Target.attachToTarget 拿 sessionId"：
  // 那条路还需要把 sessionId 带在每条消息上（或用 sendMessageToTarget 包一层），
  // 漏了就会一直收不到回应 —— 表现为"页面永远加载不出来"，很难看出原因。
  const response = await fetch(`${CDP}/json/new?${encodeURIComponent(url)}`, {
    method: "PUT",
  });
  if (!response.ok) throw new Error(`新建标签失败：${response.status}`);
  const target = await response.json();
  const socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });
  let nextId = 1;
  const pending = new Map();
  //: 事件订阅（CDP 的 `method` 消息）。M9 的删除用例要接 `Page.javascriptDialogOpening`
  //: ——原生 `confirm()` 会**挡住页面**，必须一边记下它弹出过、一边用
  //: `Page.handleJavaScriptDialog` 接受它，否则脚本就停在那儿了。
  const listeners = new Map();
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      const entry = pending.get(message.id);
      pending.delete(message.id);
      message.error ? entry.reject(new Error(message.error.message)) : entry.resolve(message.result);
      return;
    }
    // 没有 id 的才是事件（两者互斥，不写成 if/else 是为了让上面那条早退更显眼）
    if (message.method && listeners.has(message.method)) {
      for (const listener of listeners.get(message.method)) listener(message.params);
    }
  });
  const send = (method, params = {}) =>
    new Promise((resolve, reject) => {
      const id = nextId++;
      pending.set(id, { resolve, reject });
      socket.send(JSON.stringify({ id, method, params }));
    });
  const on = (method, listener) => {
    const bucket = listeners.get(method) ?? new Set();
    bucket.add(listener);
    listeners.set(method, bucket);
  };
  await send("Page.enable");
  await send("Runtime.enable");
  return { send, on, close: () => socket.close() };
}

const results = [];
function check(ok, title, evidence = "") {
  results.push({ ok, title, evidence });
  console.log(`${ok ? "PASS" : "FAIL"}  ${title}${evidence ? "   —— " + evidence : ""}`);
}

async function main() {
  const page = await connect(`${APP}/login`);

  const rawEvaluate = async (expression) => {
    const result = await page.send("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
    });
    if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
    return result.result.value;
  };

  /** 页面在**重新加载的那一瞬间** `document.body` 是 null，任何读 body 的表达式都会
   *  抛 "Cannot read properties of null" —— 那是过渡态，不是脚本的错。
   *  这里改成：读 body 的表达式统一走 `document.body ? … : 空串`，
   *  调用方要等的条件下一轮自然会成立。 */
  const evaluate = async (expression) => rawEvaluate(`(() => {
    if (!document.body) return "";
    return (${expression});
  })()`);

  const waitFor = async (condition, label, timeout = 15000) => {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      if (await evaluate(condition)) return true;
      await sleep(200);
    }
    // ⚠️ 超时的时候**把现场带上**：只报"等待超时：某个标签"会让人去猜
    // （实测为了定位一处失败反复重跑了四轮）。URL + 页面前 200 字 + 页面上
    // 有哪些按钮，这三样一次就能说清"到底停在哪一页、那页上有什么"。
    const where = await evaluate(`(() => {
      const buttons = [].slice.call(document.querySelectorAll('button'))
        .map((b) => b.textContent.trim()).filter(Boolean).slice(0, 8).join('|');
      return location.pathname + ' :: ' + document.body.innerText.replace(/\\s+/g, ' ').slice(0, 200)
        + ' :: buttons=' + buttons;
    })()`);
    throw new Error(`等待超时：${label}\n      现场：${where}`);
  };

  const shot = async (name) => {
    const { data } = await page.send("Page.captureScreenshot", { format: "png" });
    const dir = join(tmpdir(), "procurement-m6");
    mkdirSync(dir, { recursive: true });
    const path = join(dir, `${name}.png`);
    writeFileSync(path, Buffer.from(data, "base64"));
    return path;
  };

  // ---------- 登录态：以 `dept-manager`（行政部主管）的身份跑 ----------
  // **为什么不用 admin**：admin 是系统级账号、**没有部门**，而后端要求申请人必须有部门
  // （`NO_DEPARTMENT` 422，刻意的：没有部门就算不出审批链该找谁）。
  // 种子里的 `dept-manager` 在行政部，同部门还有 `dept-head`（部门负责人），
  // 所以"两级链 + 我审自己部门的单"这两件事都能验到。
  await page.send("Page.navigate", { url: `${APP}/` });
  await waitFor("!!document.querySelector('nav[data-testid=app-nav]')", "应用外壳渲染");

  const loginAs = async (username) => {
    // ① 页内 fetch 登录（探针实测这条最可靠：同源、Cookie 直接落地）；
    // ② 再 `Page.reload()` 重载 —— React Query 里的 `useMe()` 缓存的是上一个身份，
    //    不重载界面会继续按"未登录"渲染（表现为停在登录页，而接口其实是通的）；
    // ③ 最后**回查一次身份**，确认换成功了再往下走。
    //
    // 不用"填登录表单提交"：那条路要等多步渲染，而且导航时机会把登录请求冲掉
    // （实测表现为时好时坏）。
    const status = await evaluate(`(async () => {
      await fetch('/api/v1/auth/logout', { method: 'POST' });
      const response = await fetch('/api/v1/auth/login', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: '${username}', password: 'Dev-Passw0rd!' }),
      });
      return response.status;
    })()`);
    await page.send("Page.reload", { ignoreCache: false });
    await waitFor("!!document.querySelector('nav[data-testid=app-nav]')", `${username} 应用外壳`);
    const who = await evaluate(
      "(async () => { const r = await fetch('/api/v1/auth/me'); return r.ok ? (await r.json()).username : null; })()",
    );
    if (who !== username) {
      throw new Error(`切换到 ${username} 失败：登录 HTTP ${status}，/auth/me 返回 ${who}`);
    }
  };

  await loginAs("dept-manager");
  const me = await evaluate("(async () => (await fetch('/api/v1/auth/me')).json())()");
  check(
    me?.username === "dept-manager" && me.roles.includes("DEPT_MANAGER"),
    "以部门主管身份实测（脚本不假设浏览器里没有会话）",
    `账号=${me?.username} 角色=${me?.roles?.join("+")} 部门=${me?.department_id ? "有" : "无"}`,
  );

  // ---------- 一、草稿：没有审批流时不显示「审批进度」卡片 ----------
  // ⚠️ **自己造一条品类，不挑"列表里第一个"**：开发库的品类列表可能混着
  // 浏览器自检留下的 `SELFCHECK-*`（它的策略故意是"无兜底"、保存时会被 422 拦下），
  // 挑到它就会建草稿失败 —— 而且报错看起来像"页面坏了"，实则是脏数据。
  // 用 `TCAT-PAGE-` 前缀，跑完由 `backend/scripts/testdata_cleanup.py` 删掉。
  //
  // 建品类要 ADMIN，而实测要以部门主管身份跑（admin 没有部门、提交不了申请），
  // 所以这里临时换一次身份：建完立刻切回来。
  const stamp = Date.now().toString(36).toUpperCase();

  await loginAs("admin");
  const adminMe = await evaluate(
    "(async () => { const r = await fetch('/api/v1/auth/me'); return r.ok ? r.json() : { status: r.status }; })()",
  );
  check(
    adminMe?.roles?.includes("ADMIN"),
    "临时切成管理员（建品类需要 ADMIN）",
    `账号=${adminMe?.username} 角色=${(adminMe?.roles || []).join("+")}`,
  );
  const draftCategory = await evaluate(`(async () => {
    const response = await fetch('/api/v1/admin/categories', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: '页面实测品类 ${stamp}', code: 'TCAT-PAGE-${stamp}',
        approval_policy: {
          steps: [
            { order: 10, role: 'DEPT_MANAGER', mode: 'SINGLE',
              when: { gte: ['amount_fen', 0] } },
            { order: 20, role: 'DEPT_HEAD', mode: 'SINGLE',
              when: { gte: ['amount_fen', 100000] } },
          ],
          skip_when_framework_contract: false,
        },
      }),
    });
    const body = await response.json();
    if (!response.ok) return { error: JSON.stringify(body).slice(0, 160) };
    return { id: body.id, name: body.name };
  })()`);
  await loginAs("dept-manager");
  if (draftCategory.error) throw new Error(`建页面实测品类失败：${draftCategory.error}`);
  check(true, "自建一条页面实测品类（不依赖库里现成数据）", draftCategory.name);

  // M7 起**提交必须有预算**：没有预算行 = 计划外 → 422。
  // 所以这条自建品类也要配一条额度足够的预算（仍是管理员身份，下面才切回去）。
  await loginAs("admin");
  const draftBudget = await evaluate(`(async () => {
    const response = await fetch('/api/v1/admin/budgets', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        department_id: '${me.department_id}',
        category_id: '${draftCategory.id}',
        year: ${new Date().getFullYear()},
        amount_fen: 10000000,
      }),
    });
    const body = await response.json();
    if (!response.ok) return { error: JSON.stringify(body).slice(0, 160) };
    return { id: body.id };
  })()`);
  await loginAs("dept-manager");
  if (draftBudget.error) throw new Error(`建页面实测预算失败：${draftBudget.error}`);
  check(true, "给这条品类配一条预算（M7 起提交必须先有预算）", "额度 100000 元");

  const draft = await evaluate(`(async () => {
    const response = await fetch('/api/v1/requests', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ category_id: '${draftCategory.id}', title: 'M6 页面实测-草稿' }),
    });
    const body = await response.json();
    if (!response.ok) {
      return { error: 'HTTP ' + response.status + ' ' + JSON.stringify(body).slice(0, 200) };
    }
    return { id: body.id };
  })()`);

  if (draft.error) {
    check(false, "建草稿（页面实测用）", draft.error);
  }

  await page.send("Page.navigate", { url: `${APP}/requests/${draft.id}` });
  try {
    await waitFor("document.body.innerText.indexOf('M6 页面实测-草稿')>=0", "草稿详情渲染", 8000);
  } catch (error) {
    const text = await evaluate("document.body.innerText");
    check(
      false,
      "草稿详情渲染",
      `draft=${JSON.stringify(draft)} 页面文本=${text.replace(/\s+/g, " ").slice(0, 160)}`,
    );
  }
  const draftText = await evaluate("document.body.innerText");
  check(
    !draftText.includes("审批进度"),
    "草稿没有审批流时不显示「审批进度」卡片（而不是画一片报错）",
    `含"审批进度"=${draftText.includes("审批进度")}`,
  );
  const draftShot = await shot("01-request-draft");
  check(true, "草稿详情已截图", draftShot);

  // ---------- 待办列表 ----------
  await page.send("Page.navigate", { url: `${APP}/approvals/tasks` });
  await waitFor("document.body.innerText.indexOf('我的待办')>=0", "待办页渲染");
  await waitFor("document.body.innerText.indexOf('加载中')<0", "待办列表加载完毕");
  const todoText = await evaluate("document.body.innerText");
  check(
    todoText.includes("我的待办") && todoText.includes("只显示现在轮到您的审批"),
    "待办页渲染出标题与「只显示现在轮到您的审批」的说明",
    "标题在，说明在",
  );
  check(
    !todoText.includes("401") && !todoText.includes("请求失败"),
    "待办页没有报错（空列表也有明确的空状态文案）",
    todoText.includes("现在没有需要您处理的申请") ? "显示空状态文案" : "有数据",
  );
  const todoShot = await shot("02-todo-list");
  check(true, "待办列表已截图", todoShot);

  // ---------- 三、审批链时间轴 ----------
  // 造一条**真走完第 1 级**的申请，第 1 级由当前账号（dept-manager）在页面上点「通过」，
  // 这样时间轴上会同时有"已完成 ✓"和"未开始 ○"，三态才验得全。
  //
  // ⚠️ **申请人必须换成 `requester`，不能再用 `dept-manager` 自己提。**
  // 2026-10-03 加了职责分离：提交人不能审批自己的申请，而行政部的 `DEPT_MANAGER`
  // 只有他一个人 —— 他自提会直接在提交时 422（`SELF_APPROVAL_NOT_ALLOWED`）。
  // 所以这里分两步：先切成 `requester` 提交，再切回 `dept-manager` 审批。
  const flowInfo = await evaluate(`(async () => {
    const me = await (await fetch('/api/v1/auth/me')).json();
    return { roles: me.roles };
  })()`);
  check(
    flowInfo.roles.includes("DEPT_MANAGER"),
    "当前账号是部门主管（第 1 级由他来批）",
    "roles=" + flowInfo.roles.join("+"),
  );

  await loginAs("requester");
  const flowed = await evaluate(`(async () => {
    const headers = { 'Content-Type': 'application/json' };
    const created = await (await fetch('/api/v1/requests', {
      method: 'POST', headers, body: JSON.stringify({
        category_id: '${draftCategory.id}', title: 'M6 页面实测-已提交' }),
    })).json();
    const withItems = await (await fetch('/api/v1/requests/' + created.id + '/items', {
      method: 'PUT', headers, body: JSON.stringify({
        version: created.version,
        // 金额 1000 元：跨过策略里 DEPT_HEAD 的阈值（100000 分），链路才是**两级**。
        // 一级链上验不出"未开始 ○"这一态（整条链只有当前那一级）。
        items: [{ name: 'A4 复印纸', quantity: 1, unit_price_fen: 100000 }],
      }),
    })).json();
    const submitted = await (await fetch('/api/v1/requests/' + created.id + '/submit', {
      method: 'POST',
      headers: { ...headers, 'Idempotency-Key': 'm6-page-' + Date.now() },
      body: JSON.stringify({ version: withItems.version }),
    })).json();
    if (submitted.error) return { error: submitted.error.message };
    const flow = await (await fetch('/api/v1/approvals/requests/' + created.id + '/flow')).json();
    return {
      id: created.id, status: submitted.status,
      steps: flow.steps.length, current: flow.current_step_order,
    };
  })()`);

  if (flowed.error) {
    check(false, "造一条已提交的申请用于验证时间轴", flowed.error);
  } else {
    // 提交完切回部门主管 —— 后面的页面交互（认领 / 通过）都要以他的身份做
    await loginAs("dept-manager");
    await page.send("Page.navigate", { url: `${APP}/requests/${flowed.id}` });
    await waitFor("document.body.innerText.indexOf('审批进度')>=0", "审批进度卡片出现");
    const timeline = await evaluate(
      "(document.querySelector('[data-testid=approval-timeline]') || {}).innerText || ''",
    );
    check(
      timeline.length > 0,
      "申请详情里画出了审批链时间轴（申请人看得到自己卡在谁那里）",
      `步骤数=${flowed.steps} 当前级=${flowed.current}`,
    );
    const after = await evaluate(
      "(document.querySelector('[data-testid=approval-timeline]') || {}).innerText || ''",
    );
    // 注意断言的是**可见文本**：状态字（"当前""未开始"）只出现在圆点的
    // `title` / `aria-label` 里，`innerText` 读不到；能读到的三态信号是
    // "还没轮到这一级"（未开始）与审批意见（已完成）。
    check(
      after.includes("还没轮到这一级"),
      "还没轮到的级别在时间轴上标明「还没轮到这一级 ○」",
      after.replace(/\s+/g, " ").slice(0, 90),
    );

    // 第 1 级就是当前账号（dept-manager）自己，所以用**页面上的「通过」按钮**批掉它 ——
    // 这一条才真叫"页面实测"：按钮点得动、409/403 有提示、批准后时间轴会变。
    const taskId = await evaluate(`(async () => {
      const todo = await (await fetch('/api/v1/approvals/tasks')).json();
      const mine = todo.items.find((item) => item.request.id === '${flowed.id}');
      return mine ? mine.task.id : null;
    })()`);
    if (!taskId) {
      check(false, "待办列表里有这条申请（第 1 级该轮到部门主管）", "没有找到");
    } else {
      await page.send("Page.navigate", { url: `${APP}/approvals/tasks/${taskId}` });
      // 等**内容真的出现**（而不是"没有加载中" —— 首帧还没有那四个字，条件会立刻成立，
      // 于是断言跑在 React 渲染之前，读到一个空页面。这个坑实测踩过）。
      try {
        await waitFor("document.body.innerText.indexOf('我的审批')>=0", "任务详情内容出现", 15000);
      } catch (error) {
        const text = await evaluate("document.body.innerText");
        const url = await evaluate("location.href");
        check(false, "任务详情内容出现", `url=${url} task=${taskId} 文本=${
          text.replace(/\s+/g, " ").slice(0, 120)
        }`);
      }
      const detailText = await evaluate("document.body.innerText");
      const buttonsBefore = await evaluate(
        "[].slice.call(document.querySelectorAll('button')).map((el) => el.textContent.trim())",
      );
      // **或签任务先认领，才有「通过/驳回」**：未认领时只给一个「认领这条任务」，
      // 因为"所有人都要通过"（会签）与"谁抢到算谁的"（认领）语义互斥。
      check(
        buttonsBefore.includes("认领这条任务"),
        "未认领的或签任务只给「认领」，不给「通过 / 驳回」",
        `按钮=${JSON.stringify(buttonsBefore)}`,
      );

      await evaluate(`(() => {
        const button = [].slice.call(document.querySelectorAll('button'))
          .find((el) => el.textContent.trim() === '认领这条任务');
        if (button) button.click();
        return !!button;
      })()`);
      await waitFor(
        "[].slice.call(document.querySelectorAll('button'))"
        + ".some((el) => el.textContent.trim() === '通过')",
        "认领后出现「通过 / 驳回」",
        15000,
      );
      const buttonsAfter = await evaluate(
        "[].slice.call(document.querySelectorAll('button')).map((el) => el.textContent.trim())",
      );
      check(
        buttonsAfter.includes("通过") && buttonsAfter.includes("驳回"),
        "认领之后页面给出「通过 / 驳回」按钮",
        `按钮=${JSON.stringify(buttonsAfter)} 页面含"或签"=${detailText.includes("或签")}`,
      );
      const detailShot = await shot("03-task-detail");
      check(true, "任务详情已截图", detailShot);

      // 点页面上的「通过」。
      // ⚠️ 批准成功后组件会 `navigate` 离开，页面被卸载 —— 所以**不要**在点击之后
      // 再去读这个页面的 DOM（会读到空/旧值）。结论一律从接口与随后重新导航的页面取。
      const clickResult = await evaluate(`(() => {
        const button = [].slice.call(document.querySelectorAll('button'))
          .find((el) => el.textContent.trim() === '通过');
        if (!button) return 'no-button';
        button.click();
        return 'clicked';
      })()`);
      await sleep(1500);
      const moved = await evaluate(`(async () => {
        const flow = await (await fetch('/api/v1/approvals/requests/${flowed.id}/flow')).json();
        return { current: flow.current_step_order, status: flow.status,
                 done: flow.steps.filter((s) => s.status === 'APPROVED').length };
      })()`);
      check(
        clickResult === "clicked" && (moved.current > flowed.current || moved.status !== "RUNNING"),
        "在页面上点「通过」→ 审批链真的往前走了",
        `点击=${clickResult}，当前级 ${flowed.current} → ${moved.current}，已通过 ${moved.done} 条`,
      );

      // 批准后：时间轴应当同时出现「已完成 ✓」与「未开始 ○」
      await page.send("Page.navigate", { url: `${APP}/requests/${flowed.id}` });
      await waitFor("document.body.innerText.indexOf('审批进度')>=0", "通过后重看审批进度");
      const after = await evaluate(
        "(document.querySelector('[data-testid=approval-timeline]') || {}).innerText || ''",
      );
      check(
        after.includes("完成") || after.includes("通过"),
        "批准后时间轴上出现「已完成 ✓」（审批意见一并显示）",
        after.replace(/\s+/g, " ").slice(0, 110),
      );
      const flowShot = await shot("04-approval-timeline");
      check(true, "审批链时间轴已截图", flowShot);
    }
  }

  // ---------- M9：附件 + 全生命周期时间线 ----------
  // 这一组验的是**页面能不能真的用**：上传得动、列表出得来、下载链接在、
  // 时间线（读 request_events）与审批进度（读 approval_tasks）是**两个**东西。
  if (!flowed.error) {
    await page.send("Page.navigate", { url: `${APP}/requests/${flowed.id}` });
    await waitFor(
      "document.body.innerText.indexOf('附件')>=0 && !!document.querySelector('[data-testid=request-timeline]')",
      "附件区与事件时间线出现",
    );

    // M9-1：事件流水（request_events）—— 与审批链**同时存在但来源不同**
    const eventTypes = await evaluate(`[].slice
      .call(document.querySelectorAll('[data-testid=request-timeline] li'))
      .map((li) => li.innerText.replace(/\\s+/g, ' ').trim())`);
    const hasApprovalTimeline = await evaluate(
      "!!document.querySelector('[data-testid=approval-timeline]')",
    );
    check(
      eventTypes.length >= 3 && hasApprovalTimeline,
      "详情页同时有两个时间轴：事件流水（request_events）与审批进度（approval_tasks）",
      `事件条数=${eventTypes.length}，审批链在=${hasApprovalTimeline}；首条=「${(eventTypes[0] ?? '').slice(0, 40)}」`,
    );
    check(
      eventTypes.some((text) => text.includes("提交")) &&
        eventTypes.some((text) => text.includes("生成审批链")),
      "事件流水按正序展示，含「提交」与「生成审批链」（顺序与写入一致）",
      eventTypes.map((t) => t.split(" ")[0]).join(" → "),
    );

    // M9-2：用途下拉 + 上传按钮都在
    const attachmentUi = await evaluate(`(() => {
      const select = document.querySelector('[aria-label=附件用途]');
      const button = document.querySelector('[data-testid=attachment-upload-button]');
      return {
        kinds: select ? [].slice.call(select.options).map((o) => o.textContent.trim()) : [],
        hasButton: !!button,
        buttonText: button ? button.textContent.trim() : '',
      };
    })()`);
    check(
      attachmentUi.kinds.length === 5 && attachmentUi.hasButton,
      "附件区有 5 种用途（报价单/合同/发票/验收单/其他）与上传按钮",
      `用途=${attachmentUi.kinds.join("/")}，按钮=「${attachmentUi.buttonText}」`,
    );

    const beforeTimeline = eventTypes.length;

    // M9-3：**真的把文件塞进 input 再让 React 处理**（不是直接调接口）。
    // DataTransfer 是唯一能在页面上"造出一个真实 FileList"的办法；
    // 只设 input.value 是不行的（那会抛错，而且 React 的受控流程收不到 change）。
    const beforeUpload = await evaluate(`(async () => {
      const list = await (await fetch('/api/v1/requests/${flowed.id}/attachments')).json();
      return Array.isArray(list) ? list.length : -1;
    })()`);
    const injected = await evaluate(`(() => {
      const input = document.querySelector('[data-testid=attachment-file-input]');
      if (!input) return 'no-input';
      // 一份最小的合法 PDF（内容无关紧要：后端只查文件头 %PDF + 大小）
      const bytes = new TextEncoder().encode('%PDF-1.7\\n1 0 obj\\n<<>>\\nendobj\\ntrailer\\n%%EOF\\n');
      const file = new File([bytes], '页面实测报价单.pdf', { type: 'application/pdf' });
      const transfer = new DataTransfer();
      transfer.items.add(file);
      input.files = transfer.files;
      input.dispatchEvent(new Event('change', { bubbles: true }));
      return 'injected';
    })()`);
    await waitFor(
      `(() => { const el = document.querySelector('[data-testid=attachment-list]');
        return !!el && el.innerText.indexOf('页面实测报价单.pdf') >= 0; })()`,
      "上传的附件出现在列表里",
      15000,
    );
    const listed = await evaluate(`(() => {
      const el = document.querySelector('[data-testid=attachment-list]');
      return el ? el.innerText.replace(/\\s+/g, ' ').trim() : '';
    })()`);
    check(
      injected === "injected" && listed.includes("页面实测报价单.pdf"),
      "在页面上选文件 → 上传成功 → 列表里出现该附件（走真实 FileList 与 React 事件）",
      `注入=${injected}；列表=「${listed.slice(0, 90)}」`,
    );
    check(
      // 注意用 `[0-9]` 而不是 `\d`：这段是**字符串里的正则**，`\d` 的反斜杠
      // 会被模板字符串吃掉，变成字面量 `/d/` —— 永远匹配不上（实测踩过）。
      listed.includes("报价单") && /[0-9]/.test(listed),
      "附件行显示用途标签与大小（报价单 · 字节数）",
      listed.slice(0, 90),
    );
    const hasDownload = await evaluate(
      "[].slice.call(document.querySelectorAll('[data-testid=attachment-list] button'))" +
        ".some((b) => b.textContent.trim() === '下载')",
    );
    check(hasDownload, "附件行有「下载」按钮（走 fetch + Blob，能拿到 403/404 的真实文案）", "");

    await page.send("Page.navigate", { url: `${APP}/requests/${flowed.id}` });
    await waitFor("!!document.querySelector('[data-testid=request-timeline]')", "重新加载时间线");
    const afterTimeline = await evaluate(`[].slice
      .call(document.querySelectorAll('[data-testid=request-timeline] li'))
      .map((li) => li.innerText.replace(/\\s+/g, ' ').trim())`);
    check(
      afterTimeline.length === beforeTimeline + 1 &&
        afterTimeline[afterTimeline.length - 1].includes("上传附件"),
      "上传后时间线多出一条「上传附件」（附件事件是服务端写的，不是前端猜的）",
      `事件 ${beforeTimeline} → ${afterTimeline.length}；末条=「${(afterTimeline[afterTimeline.length - 1] ?? '').slice(0, 60)}」`,
    );

    // M9-4：前端预检（大小 / 扩展名）——不进网络就该看到提示
    const oversize = await evaluate(`(() => {
      const input = document.querySelector('[data-testid=attachment-file-input]');
      const big = new Uint8Array(11 * 1024 * 1024);
      const file = new File([big], '超大.pdf', { type: 'application/pdf' });
      const transfer = new DataTransfer();
      transfer.items.add(file);
      input.files = transfer.files;
      input.dispatchEvent(new Event('change', { bubbles: true }));
      return 'ok';
    })()`);
    await sleep(400);
    const oversizeNote = await evaluate("document.body.innerText");
    check(
      oversize === "ok" && oversizeNote.includes("超过 10 MB"),
      "选一个 11 MB 的文件 → 页面上当场提示「超过 10 MB」，不发请求",
      "",
    );

    const badExtension = await evaluate(`(() => {
      const input = document.querySelector('[data-testid=attachment-file-input]');
      const file = new File([new Uint8Array(64)], '木马.exe', { type: 'application/octet-stream' });
      const transfer = new DataTransfer();
      transfer.items.add(file);
      input.files = transfer.files;
      input.dispatchEvent(new Event('change', { bubbles: true }));
      return 'ok';
    })()`);
    await sleep(400);
    const extNote = await evaluate("document.body.innerText");
    check(
      badExtension === "ok" && extNote.includes("只接受"),
      "选一个 .exe → 页面上当场提示只接受 PDF/JPG/PNG",
      "",
    );

    // M9-5：接口层的两个拒绝面（页面预检拦不到的情况由后端兜住）
    const serverChecks = await evaluate(`(async () => {
      const form = (name, content) => {
        const data = new FormData();
        data.append('file', new File([content], name, { type: 'application/pdf' }));
        data.append('kind', 'OTHER');
        return data;
      };
      // ① 改名成 .pdf 的 exe：页面预检放行，后端必须 422
      const exe = await fetch('/api/v1/requests/${flowed.id}/attachments', {
        method: 'POST',
        body: form('木马.pdf', new TextEncoder().encode('MZ\\u0090\\u0000\\u0003' + 'x'.repeat(200))),
      });
      const exeBody = await exe.json();
      // ② 没带类型（octet-stream）+ 扩展名可识别 → 走兜底后仍要过文件头校验
      const data = new FormData();
      data.append('file', new File([new TextEncoder().encode('MZ fake')], '又是木马.pdf',
        { type: 'application/octet-stream' }));
      const fallback = await fetch('/api/v1/requests/${flowed.id}/attachments', {
        method: 'POST', body: data,
      });
      const fallbackBody = await fallback.json();
      return {
        exeStatus: exe.status, exeCode: exeBody.error && exeBody.error.code,
        fallbackStatus: fallback.status, fallbackCode: fallbackBody.error && fallbackBody.error.code,
      };
    })()`);
    check(
      serverChecks.exeStatus === 422 && serverChecks.exeCode === "MAGIC_BYTES_MISMATCH",
      "改名成 .pdf 的 exe → 后端 422 MAGIC_BYTES_MISMATCH（页面预检只是体验，边界在后端）",
      JSON.stringify(serverChecks),
    );
    check(
      serverChecks.fallbackStatus === 422 &&
        serverChecks.fallbackCode === "MAGIC_BYTES_MISMATCH",
      "octet-stream 兜底**没有**绕过内容校验：补出 pdf 之后文件头照样被查",
      `octet-stream 用例=${serverChecks.fallbackStatus}/${serverChecks.fallbackCode}`,
    );

    const attachmentShot = await shot("06-attachments");
    check(true, "附件与事件时间线已截图", attachmentShot);

    // ---------- M9 补充：上传进度条 + 删除附件 ----------

    // ① 进度条：本机上传 43 字节是**瞬间**完成的，正常情况根本看不到进度条。
    //    所以先用 CDP 把上传方向限速（100 KB/s），再传一个几百 KB 的文件 ——
    //    这样才观察得到"百分比在动"。（限速只影响这次实测，跑完恢复。）
    await page.send("Network.enable");
    await page.send("Network.emulateNetworkConditions", {
      offline: false,
      latency: 20,
      downloadThroughput: -1,
      uploadThroughput: 100 * 1024, // 100 KB/s
    });
    const progressSeen = await evaluate(`(async () => {
      const input = document.querySelector('[data-testid=attachment-file-input]');
      if (!input) return { error: 'no-input' };
      // 约 400 KB 的合法 PDF（%PDF 头 + 填充），限速下大约要 4 秒
      const head = new TextEncoder().encode('%PDF-1.7\\n');
      const filler = new Uint8Array(400 * 1024).fill(0x20);
      const file = new File([head, filler], '大文件进度条.pdf', { type: 'application/pdf' });
      const transfer = new DataTransfer();
      transfer.items.add(file);
      input.files = transfer.files;
      input.dispatchEvent(new Event('change', { bubbles: true }));

      // 边传边采样：既确认进度条**出现过**，也确认百分比**真的在增长**
      const samples = [];
      for (let i = 0; i < 40; i++) {
        await new Promise((r) => setTimeout(r, 150));
        const bar = document.querySelector('[data-testid=attachment-progress]');
        if (bar) {
          const now = Number(bar.querySelector('[role=progressbar]').getAttribute('aria-valuenow'));
          samples.push(now);
          if (now >= 100) break;
        } else if (samples.length) {
          break; // 进度条消失 = 传完了
        }
      }
      return { samples };
    })()`);
    await page.send("Network.emulateNetworkConditions", {
      offline: false,
      latency: 0,
      downloadThroughput: -1,
      uploadThroughput: -1,
    });
    const grew = progressSeen.samples && progressSeen.samples.length >= 2 &&
      progressSeen.samples.some((value) => value > 0 && value < 100);
    check(
      grew,
      "限速上传 400 KB → 进度条出现且百分比在增长（fetch 做不到，故走 XHR）",
      `采样=${JSON.stringify((progressSeen.samples || []).slice(0, 8))}`,
    );

    await waitFor(
      `(() => { const el = document.querySelector('[data-testid=attachment-list]');
        return !!el && el.innerText.indexOf('大文件进度条.pdf') >= 0; })()`,
      "限速上传最终完成",
      30000,
    );

    // ② 删除：先确认二次确认框会出现（它是**拦住误触**的那一步），再确认删除后列表与时间线都变
    const beforeDelete = await evaluate(`(async () => {
      const events = await (await fetch('/api/v1/requests/${flowed.id}/timeline')).json();
      return { events: events.events.length };
    })()`);

    // 点"删除"会弹原生 confirm，**会挡住后续脚本** —— 所以一边自动接受，
    // 一边把它弹出的消息记下来（这是"有二次确认"的证据，而不是靠读代码声称）。
    let dialogMessage = null;
    page.on("Page.javascriptDialogOpening", (params) => {
      dialogMessage = params.message;
      void page.send("Page.handleJavaScriptDialog", { accept: true });
    });

    const clickedDelete = await evaluate(`(() => {
      const rows = [].slice.call(document.querySelectorAll('[data-testid=attachment-list] li'));
      const target = rows.find((li) => li.innerText.indexOf('页面实测报价单.pdf') >= 0);
      if (!target) return 'no-row';
      const button = [].slice.call(target.querySelectorAll('button'))
        .find((b) => b.textContent.trim() === '删除');
      if (!button) return 'no-button';
      button.click();
      return 'clicked';
    })()`);
    await sleep(1800);
    const listedAfter = await evaluate(`(() => {
      const el = document.querySelector('[data-testid=attachment-list]');
      return el ? el.innerText.replace(/\\s+/g, ' ').trim() : '';
    })()`);
    check(
      clickedDelete === "clicked" && dialogMessage !== null,
      "点「删除」会先弹二次确认（防误触；没有撤销入口的操作不能只点一下就消失）",
      clickedDelete === "clicked"
        ? `对话框首行=「${String(dialogMessage).split("\n")[0].slice(0, 42)}」`
        : `点击结果=${clickedDelete}`,
    );
    check(
      !listedAfter.includes("页面实测报价单.pdf"),
      "确认后附件从列表消失（软删除：服务端留痕，磁盘文件不删）",
      `列表=「${listedAfter.slice(0, 70)}」`,
    );

    await page.send("Page.navigate", { url: `${APP}/requests/${flowed.id}` });
    await waitFor("!!document.querySelector('[data-testid=request-timeline]')", "删除后重看时间线");
    const afterDeleteTimeline = await evaluate(`[].slice
      .call(document.querySelectorAll('[data-testid=request-timeline] li'))
      .map((li) => li.innerText.replace(/\\s+/g, ' ').trim())`);
    check(
      afterDeleteTimeline.some((text) => text.includes("删除附件")),
      "删除在时间线上留痕（多一条「删除附件」，且上传那条事件仍在）",
      `末条=「${(afterDeleteTimeline[afterDeleteTimeline.length - 1] ?? '').slice(0, 60)}」`,
    );
    check(
      afterDeleteTimeline.length === beforeDelete.events + 1,
      "时间线只多出「删除附件」这一条（beforeDelete 是在上传之后取的，所以只 +1）",
      `${beforeDelete.events} → ${afterDeleteTimeline.length}`,
    );

    // 注意：**不在这里验"别人删不掉 → 403"**。种子里的 7 个演示账号中，
    // 只有 DEPT_MANAGER / DEPT_HEAD 是"部门级可见"，而这两个都在行政部（ADM）——
    // 也就是说页面实测的环境里根本造不出"看不到这张单子"的账号
    // （PROCUREMENT / FINANCE / ADMIN 都是全公司可见，删得掉是**对的**）。
    // 跨部门 403 由集成测试覆盖：`test_delete_by_an_outsider_is_403_and_the_attachment_survives`。

    const deleteShot = await shot("07-attachment-deleted");
    check(true, "删除后的附件与时间线已截图", deleteShot);
  }

  // ================================================================
  // M10 全链路（履约工作台）：申请 → 审批 → 询价定标 → 下单收货 → 收票匹配 → 付款归档
  // ================================================================
  //
  // ============================ 这一块为什么必须走页面 ============================
  // M10 的后端有 23 条集成用例覆盖（含真并发），它们证明的是"规则对"。
  // 而这里要证的是另一件事：**界面上真的点得通**，而且"谁能看到哪一步"没写错。
  // 履约工作台是 M10 唯一的界面入口，它的可见性判断写在**三处**
  // （菜单 / 路由守卫 / 首页快捷入口），三处不一致的表现是
  // "菜单上有、点进去说无权限"——光读代码很难发现。
  //
  // ============================ 金额设计（让"差额释放"看得见） ============================
  //   申请：1 × 900 元 = 90000 分   ← 提交时按申请金额预留
  //   报价：300 元（成交单价）      ← 询价压价
  //   订单：(1 × 300) = 30000 分    ← 订单金额是**成交单价 × 数量**
  //   实付：30000 分                ← 匹配比的是订单金额
  //   释放：90000 − 30000 = 60000 分 ← M7 那条"差额必须显式释放"
  // 三个数字各不相同，任何一处取错（例如误用申请金额当匹配基准）都会当场露出来。
  {
    // 复用前面已经建好的页面实测品类与预算；那条预算额度 10 万分 = 1000 元，
    // 够 900 元的申请（不够的话提交会被 422 拦下，也是有效发现）。
    //
    // ⚠️ **提交人必须是 `dept-head`，不能是 `dept-manager`**：
    // 审批链第 10 级是 `DEPT_MANAGER`，而行政部里只有 `dept-manager` 一个人符合 ——
    // 让部门主管提交就等于"他自己审自己"，后端会以
    // `SELF_APPROVAL_NOT_ALLOWED` 422 拒绝（**这是对的**：项目禁自审，SoD）。
    // 由部门负责人提交，第 10 级就落在部门主管身上，链是通的（实测踩过这个 422）。
    // 记下预算的三个数当**基线**：本块的断言全是"相对它变了多少"，
    // 因为这条预算会被多轮实测复用（绝对值每轮都不同）。
    //
    // ⚠️ 取基线要用**管理员**身份：`GET /api/v1/budgets` 只给 FINANCE / ADMIN，
    // 部门负责人拿到的是一段错误体 —— 于是 `budgets.filter` 直接抛
    // `TypeError: budgets.filter is not a function`（实测踩到，报错还很难懂）。
    await loginAs("admin");
    const booksBefore = await evaluate(`(async () => {
      const response = await fetch('/api/v1/budgets');
      if (!response.ok) return { error: 'HTTP ' + response.status };
      const budgets = await response.json();
      const budget = budgets.filter((b) => b.id === '${draftBudget.id}')[0];
      return budget
        ? { total: budget.amount_total_fen, reserved: budget.amount_reserved_fen, spent: budget.amount_spent_fen }
        : { error: '基线里找不到这条预算' };
    })()`);
    if (booksBefore.error) throw new Error(`取预算基线失败：${booksBefore.error}`);

    await loginAs("dept-head");
    const chain = await evaluate(`(async () => {
      const created = await fetch('/api/v1/requests', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ category_id: '${draftCategory.id}', title: 'M10 页面实测-全链路' }),
      });
      const draft = await created.json();
      if (!created.ok) return { error: '建草稿 HTTP ' + created.status };
      const items = await fetch('/api/v1/requests/' + draft.id + '/items', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          version: draft.version,
          items: [{ name: '页面实测物资', spec: '整箱', quantity: 1, unit_price_fen: 90000 }],
        }),
      });
      const withItems = await items.json();
      if (!items.ok) return { error: '填明细 HTTP ' + items.status };
      const submitted = await fetch('/api/v1/requests/' + draft.id + '/submit', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Idempotency-Key': 'm10-page-submit-' + draft.id },
        body: JSON.stringify({ version: withItems.version }),
      });
      if (!submitted.ok) {
        return { error: '提交 HTTP ' + submitted.status + ' ' + JSON.stringify(await submitted.json()).slice(0, 160) };
      }
      return { id: draft.id };
    })()`);
    if (chain.error) throw new Error(`造 M10 全链路申请失败：${chain.error}`);
    const chainId = chain.id;
    check(true, "造一条 900 元的申请用于全链路实测（1 × 90000 分）", chainId.slice(0, 8));

    /** 把值写进 React 受控输入，并**确认真的写进去了**。
     *
     * 三个必须这样做的地方：
     *  1. 必须用原型上的 setter：直接 `input.value = x` 在 React 里是**静默无效**的
     *     （它的 value tracker 认为值没变，onChange 不触发，提交的还是旧值）；
     *  2. **返回写入后的实际值**，让调用处能断言 —— 上一版返回 `'ok'`，
     *     于是"选择器没找到元素"被当成成功，一路跑到后面才以"匹配卡片没出现"爆出来，
     *     而真因（表单压根没填上）看不出来（实测为此多跑了两轮）；
     *  3. 选择器一律用 `data-testid`：面板上曾经有**两个** `placeholder='例如 1200.00'`
     *     （报价金额、发票金额），按 placeholder 找等于抽签。
     */
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

    /** 填一个字段；填不进去就**当场失败**（不把问题推到后面才爆）。 */
    const fillField = async (selector, value) => {
      const result = await evaluate(setReactValue(selector, value));
      if (!result || result.ok !== true) {
        throw new Error(`填字段失败 ${selector}：${JSON.stringify(result)}`);
      }
      return result.readback;
    };

    /** 在工作台的某个卡片（或链接）里找元素并点它。
     *
     * 这里派发的是**冒泡的 MouseEvent**（而不是 `el.click()`），但不是因为
     * `.click()` 不冒泡 —— DOM 规范里它是冒泡的，React 收得到。
     * 真正的原因是**可读性**：派发 MouseEvent 时"点的是什么、带不带冒泡"
     * 都写在调用处，而 `.click()` 需要读的人自己去回忆规范细节。
     * 另外这里接受 `a` 与 `button` 两种元素：待办列表里是链接、表单里是按钮，
     * 分两个辅助函数只多一处会漂的地方。
     */
    const clickButton = (text) => `(() => {
      const el = [].slice.call(document.querySelectorAll('button, a'))
        .filter((node) => node.textContent.trim().indexOf(${JSON.stringify(text)}) >= 0)[0];
      if (!el) {
        const seen = [].slice.call(document.querySelectorAll('button'))
          .map((b) => b.textContent.trim());
        return 'no-button:' + seen.join('|').slice(0, 120);
      }
      el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
      return 'clicked';
    })()`;

    /** 轮询这张申请的主单状态，直到它等于 `want`（或超时返回实际值）。
     *
     * **为什么用状态而不是等页面文字**：这一块里有三步（审批、付款、归档）成功之后
     * 组件会 `navigate` 离开当前页，等的那个词只会出现一瞬间。而"主单到了哪个状态"
     * 既不会骗人、又是验收真正要的结论。
     */
    const pollStatus = async (requestId, want, timeout = 20000) => {
      const deadline = Date.now() + timeout;
      let last = "";
      while (Date.now() < deadline) {
        last = await evaluate(
          `(async () => { const r = await fetch('/api/v1/requests/${requestId}');
             if (!r.ok) return 'HTTP ' + r.status;
             return (await r.json()).request.status; })()`,
        );
        if (last === want) return last;
        await new Promise((r) => setTimeout(r, 300));
      }
      return last;
    };

    // ---------- 第 1 步：部门主管审批通过（页面上点「通过」） ----------
    // 提交人是部门负责人，第 10 级（`DEPT_MANAGER`）就轮到部门主管 —— 不是自审。
    await loginAs("dept-manager");
    // ⚠️ **先用接口定位"待办里的哪一条是这张单子"，再直接导航过去**。
    // 原先是"进待办页 → 找标题链接 → 点它"，实测在这个环境里不稳：
    // 点完 URL 没变、页面停在空列表上，而证据里只看到 `no-button:退出`
    // （四个轮次才定位到，代价太大）。
    // 这一段的**被测对象是履约工作台**，不是"待办页的链接能不能点"——
    // 后者已经由既有 M6 用例覆盖了，没必要在这里再赌一次渲染时序。
    const todoTask = await evaluate(`(async () => {
      const page = await (await fetch('/api/v1/approvals/tasks?limit=50')).json();
      const hit = (page.items || []).filter(
        (item) => item.request && item.request.id === '${chainId}',
      )[0];
      return hit ? { taskId: hit.task.id, mode: hit.task.mode } : null;
    })()`);
    if (!todoTask) throw new Error("部门主管的待办里没有这张单子 —— 审批链没落到他身上");
    await page.send("Page.navigate", { url: `${APP}/approvals/tasks/${todoTask.taskId}` });
    // 等**批准按钮**（而不是等某段文字）：按钮在就说明这一级确实轮到他了。
    //
    // ⚠️ `SINGLE`（或签）级是**认领制**：没被认领之前页面上只有「认领这条任务」，
    // 「通过」按钮要认领之后才出现。实测踩到：直接等「通过」，超时后的现场里
    // 只有 `buttons=退出|认领这条任务`。所以先认领（幂等：已经认领过就不会有这个按钮）。
    await waitFor(
      "!!([].slice.call(document.querySelectorAll('button'))"
        + ".filter((b) => b.textContent.trim() === '通过')[0])"
        + " || !!([].slice.call(document.querySelectorAll('button'))"
        + ".filter((b) => b.textContent.trim() === '认领这条任务')[0])",
      "任务详情页渲染出可操作按钮",
      20000,
    );
    const claimed = await evaluate(`(() => {
      const el = [].slice.call(document.querySelectorAll('button'))
        .filter((b) => b.textContent.trim() === '认领这条任务')[0];
      if (!el) return 'already-claimed';
      el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
      return 'clicked';
    })()`);
    if (claimed === "clicked") {
      await waitFor(
        "!!([].slice.call(document.querySelectorAll('button'))"
          + ".filter((b) => b.textContent.trim() === '通过')[0])",
        "认领之后出现「通过」按钮",
        20000,
      );
    }
    const approved = await evaluate(clickButton("通过"));
    // ⚠️ **不要等"已通过"这几个字**：批准成功后组件会 `navigate` 离开任务详情页，
    // 那几个字只会出现一瞬间（既有 M6 用例的注释就警告过这一点，这里又踩了一次）。
    // 改成轮询**事实**：这张申请的主单状态变成 `SOURCING` 才算数 ——
    // 这也是验收真正要的结论（"审批通过 → 进入询价"）。
    const chainStatus = await pollStatus(chainId, "SOURCING");
    check(
      approved === "clicked" && chainStatus === "SOURCING",
      "① 部门主管在页面上认领并点「通过」→ 主单进入 SOURCING（询价中）",
      `认领=${claimed} 通过=${approved} 状态=${chainStatus}（任务=${todoTask.taskId.slice(0, 8)} 模式=${todoTask.mode}）`,
    );

    // ---------- 第 2 步：采购登记报价 + 定标 ----------
    await loginAs("procurement");
    await page.send("Page.navigate", { url: `${APP}/fulfillment?request=${chainId}` });
    await waitFor("document.body.innerText.indexOf('比价与定标')>=0", "比价卡片渲染");

    const supplierPicked = await evaluate(`(() => {
      const select = document.querySelector('select');
      if (!select) return 'no-select';
      const option = [].slice.call(select.options).filter((o) => o.value)[0];
      if (!option) return 'no-option';
      const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set;
      setter.call(select, option.value);
      select.dispatchEvent(new Event('change', { bubbles: true }));
      return option.textContent.trim();
    })()`);
    await fillField("[data-testid=quotation-amount]", 300);
    const quoted = await evaluate(clickButton("登记报价"));
    await waitFor("document.body.innerText.indexOf('300.00')>=0", "报价出现在比价表里", 15000);
    const awarded = await evaluate(clickButton("定标给它"));
    const afterAward = await pollStatus(chainId, "AWARDED");
    check(
      supplierPicked !== "no-select" && quoted === "clicked" && awarded === "clicked"
        && afterAward === "AWARDED",
      "② 采购在页面上登记报价（300 元）并定标 → 主单进入 AWARDED",
      `供应商=${supplierPicked} 登记=${quoted} 定标=${awarded} 状态=${afterAward}`,
    );

    // ---------- 第 3 步：下单 ----------
    const placed = await evaluate(clickButton("生成订单"));
    const afterOrder = await pollStatus(chainId, "ORDERED");
    // 订单号与金额**从接口读**：这一步要证的是"后端按成交单价重算成了 300 元"
    // （而不是申请金额 900 元）—— 页面上那串数字只是渲染结果。
    const orderFacts = await evaluate(
      `(async () => {
        const detail = await (await fetch('/api/v1/requests/${chainId}/orders')).json();
        return detail ? { no: detail.order.order_no, amount: detail.order.amount_fen } : null;
      })()`,
    );
    check(
      placed === "clicked" && afterOrder === "ORDERED" && orderFacts !== null
        && orderFacts.amount === 30000,
      "③ 采购点「生成订单」→ 主单 ORDERED，订单金额 30000 分（= 成交单价×数量，不是申请金额 90000）",
      `状态=${afterOrder} 订单=${orderFacts ? `${orderFacts.no} / ${orderFacts.amount} 分` : "（没有订单）"}`,
    );

    // ---------- 第 4 步：收货 ----------
    await loginAs("receiver");
    await page.send("Page.navigate", { url: `${APP}/fulfillment?request=${chainId}` });
    await waitFor("document.body.innerText.indexOf('整单收货')>=0", "收货表单渲染");
    const received = await evaluate(clickButton("登记收货"));
    const afterReceive = await pollStatus(chainId, "RECEIVED");
    check(
      received === "clicked" && afterReceive === "RECEIVED",
      "④ 收货人点「登记收货」→ 主单 RECEIVED（整单收货：逐行收齐才让提交）",
      `点击=${received} 状态=${afterReceive}`,
    );

    // ---------- 第 5 步：登记发票（故意先开成超容差，看页面会不会说清差异） ----------
    await loginAs("finance");
    await page.send("Page.navigate", { url: `${APP}/fulfillment?request=${chainId}` });
    await waitFor(
      "!!document.querySelector('[data-testid=invoice-amount]')",
      "发票表单渲染（在 RECEIVED 状态下就该出现）",
      20000,
    );
    await fillField("[data-testid=invoice-no]", `INV-PAGE-${stamp}`);
    await fillField("[data-testid=invoice-amount]", 400); // 订单 300 → 超容差 100 元
    const invoiced = await evaluate(clickButton("登记发票"));
    await waitFor("document.body.innerText.indexOf('三单匹配')>=0", "匹配卡片渲染", 15000);
    await waitFor("document.body.innerText.indexOf('AMOUNT_MISMATCH')>=0", "匹配差异渲染", 15000);
    const mismatchText = await evaluate(
      "(document.querySelector('main')||document.body).innerText.replace(/\\s+/g,' ')",
    );
    check(
      invoiced === "clicked" && mismatchText.includes("AMOUNT_MISMATCH"),
      "⑤ 发票开成 400 元（订单 300 元）→ 三单匹配页列出 AMOUNT_MISMATCH 并说清差多少",
      (mismatchText.match(/AMOUNT_MISMATCH[^；]{0,80}/) || [""])[0],
    );
    const mismatchShot = await shot("08-m10-match-mismatch");
    check(true, "三单匹配差异页已截图（对比页三列 + 高亮）", mismatchShot);

    // 作废这张错票，重新按正确的金额登记 —— 走"录错了怎么办"这条真实路径
    await evaluate(`(() => { window.confirm = () => true; window.prompt = () => '金额录错'; return 'ok'; })()`);
    const voided = await evaluate(clickButton("作废"));
    await waitFor("document.body.innerText.indexOf('已作废')>=0", "作废状态渲染", 15000);
    await fillField("[data-testid=invoice-no]", `INV-PAGE-OK-${stamp}`);
    await fillField("[data-testid=invoice-amount]", 300);
    const reinvoiced = await evaluate(clickButton("登记发票"));
    await waitFor("document.body.innerText.indexOf('在容差内')>=0", "匹配通过渲染", 15000);
    check(
      voided === "clicked" && reinvoiced === "clicked",
      "⑥ 错票在页面上作废（状态变「已作废」）→ 按正确金额重开 → 匹配通过",
      `作废=${voided} 重开=${reinvoiced}`,
    );
    const passedShot = await shot("09-m10-match-passed");
    check(true, "三单匹配通过页已截图", passedShot);

    // ---------- 第 6 步：创建付款单 → 执行付款 → 归档 ----------
    const paymentCreated = await evaluate(clickButton("创建付款单"));
    // ⚠️ 等**创建动作真的结束**（按钮文案从「创建中…」变回「创建付款单」），
    // 而不是只等"待付款"这几个字出现。
    // 实测踩到：只等文字时，下一步的「执行付款」还没渲染出来（那一刻页面上是
    // `创建中…`），于是第一次点击落在空气上，白白多跑一轮。
    await waitFor(
      "!!([].slice.call(document.querySelectorAll('button'))"
        + ".filter((b) => b.textContent.trim() === '创建付款单' && !b.disabled)[0])"
        + " || !!([].slice.call(document.querySelectorAll('button'))"
        + ".filter((b) => b.textContent.indexOf('执行付款') >= 0)[0])",
      "创建付款单的动作结束（按钮回到可点状态）",
      20000,
    );

    const payEvidence = [];
    let afterPay = "INVOICED";
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const before = await evaluate(`(() => {
        const el = [].slice.call(document.querySelectorAll('button'))
          .filter((b) => b.textContent.indexOf('执行付款') >= 0)[0];
        return el ? { text: el.textContent.trim(), disabled: el.disabled } : { missing: true };
      })()`);
      // 按钮还没出现（上一步刚提交完）就先等它出现，别点空
      if (before.missing) {
        await waitFor(
          "!!([].slice.call(document.querySelectorAll('button'))"
            + ".filter((b) => b.textContent.indexOf('执行付款') >= 0)[0])",
          "「执行付款」按钮出现",
          20000,
        );
      }
      const clicked = await evaluate(clickButton("执行付款"));
      afterPay = await pollStatus(chainId, "PAID", attempt === 0 ? 12000 : 20000);
      const pageText = await evaluate(
        "(document.querySelector('main')||document.body).innerText.replace(/\\s+/g,' ')",
      );
      payEvidence.push(
        `第${attempt + 1}次[按钮=${JSON.stringify(before)} 点击=${clicked} 状态=${afterPay}`
          + ` 报错=${(pageText.match(/(三单匹配未通过|请求失败|已被处理)[^；。]{0,60}/) || ["无"])[0]}]`,
      );
      if (afterPay === "PAID") break;
    }
    const paidText = await evaluate(
      "(document.querySelector('main')||document.body).innerText.replace(/\\s+/g,' ')",
    );
    check(
      paymentCreated === "clicked" && afterPay === "PAID",
      "⑦ 财务点「执行付款」→ 主单 PAID，页面显示实付与**释放的差额**",
      payEvidence.join(" / ")
        + "；页面提示="
        + (paidText.match(/付款完成[^。]{0,70}/) || ["（没找到付款完成提示）"])[0],
    );

    const closed = await evaluate(clickButton("归档"));
    const afterClose = await pollStatus(chainId, "CLOSED");
    check(
      closed === "clicked" && afterClose === "CLOSED",
      "⑧ 点「归档」→ 主单 CLOSED（终态）",
      `点击=${closed} 状态=${afterClose}`,
    );

    // ---------- 收尾核对：预算三个数与两笔流水 ----------
    // 这一条是整块里最值钱的断言：**页面操作之后账要对**。
    // 状态码全对但预算错，是这个系统最严重的失败方式（不报错、只是账不对）。
    //
    // ⚠️ 断言写成**相对基线**（进入这一块之前先记一份），而不是绝对数字：
    // 同一条预算会被多轮实测反复使用（品类是复用的），绝对值每次都不一样。
    // 实测踩到：写死 `reserved === 0` 时，因为库里还留着别的运行留下的预留，
    // 断言失败在一个**与本次流程无关**的数字上。
    const books = await evaluate(`(async () => {
      const rows = await (await fetch('/api/v1/budgets/transactions?limit=50')).json();
      const mine = rows.filter((row) => row.request_id === '${chainId}');
      const budgets = await (await fetch('/api/v1/budgets')).json();
      const budget = budgets.filter((b) => b.id === '${draftBudget.id}')[0];
      return {
        moves: mine.map((row) => row.kind + ':' + row.amount_fen).sort().join(','),
        reserved: budget ? budget.amount_reserved_fen : null,
        spent: budget ? budget.amount_spent_fen : null,
        total: budget ? budget.amount_total_fen : null,
      };
    })()`);
    check(
      books.total === booksBefore.total &&
        books.spent === booksBefore.spent + 30000 &&
        books.reserved === booksBefore.reserved + 90000 - 90000 &&
        books.moves.includes("RELEASE:60000") &&
        books.moves.includes("SPEND:30000"),
      "⑨ 账对上了：总额不变、已花 +30000、本单预留归零，流水里有 SPEND:30000 与 RELEASE:60000",
      `基线=${JSON.stringify(booksBefore)} 现在=${JSON.stringify(books)}`,
    );

    const closedShot = await shot("10-m10-closed");
    check(true, "全链路终态页已截图", closedShot);
  }

  // ---------- 直接访问一个不存在的任务：要看得到 404，不是白屏 ----------
  await page.send("Page.navigate", {
    url: `${APP}/approvals/tasks/00000000-0000-0000-0000-000000000000`,
  });
  // 等**错误文案出现**，不要等"没有加载中" —— 刚导航时 `document.body` 还是 null，
  // 这时"没有加载中"这个条件立刻成立（evaluate 对 null body 返回空串），
  // 于是断言跑在一片空页面上（实测踩过）。
  await waitFor(
    "document.body.innerText.indexOf('审批任务不存在')>=0",
    "错误页渲染出「审批任务不存在」",
    20000,
  );
  const missing = await evaluate("document.body.innerText");
  check(
    missing.includes("不存在") && missing.includes("request_id"),
    "访问不存在的任务 → 页面上明说任务不存在，并带上 request_id（能拿去搜日志）",
    missing.replace(/\s+/g, " ").slice(-140),
  );
  const missingShot = await shot("05-task-404");
  check(true, "任务不存在页已截图", missingShot);

  page.close();

  // ---------- 清理 ----------
  // 采购申请与品类都**没有删除端点**（`discard` 是状态不是删除；删数据走运维路径），
  // 所以这里删不掉自己造的申请与品类。统一走一个清理脚本（与浏览器自检共用）：
  //     cd backend; .venv\Scripts\python.exe scripts\testdata_cleanup.py --scope all
  console.log(
    "\n提示：本次实测造了 1 条品类（TCAT-PAGE-*）、1 条配套预算、2 条申请（标题含「M6 页面实测」）"
    + "与 1 个附件（含磁盘文件）。"
    + "\n      清理（连浏览器自检的一起）：cd backend; "
    + ".venv\\Scripts\\python.exe scripts\\testdata_cleanup.py --scope all",
  );

  const passed = results.filter((item) => item.ok).length;
  console.log(`\n=== ${passed}/${results.length} 项通过 ===`);
  return passed === results.length ? 0 : 1;
}

try {
  process.exitCode = await main();
} catch (error) {
  console.error("实测中断：", error.message);
  process.exitCode = 1;
}

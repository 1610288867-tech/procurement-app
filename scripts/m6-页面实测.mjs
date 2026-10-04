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
  return { send, close: () => socket.close() };
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
    throw new Error(`等待超时：${label}`);
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

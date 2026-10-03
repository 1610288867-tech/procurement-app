// 浏览器自检：自己起一个**无窗口**的 Edge，连它的 CDP 调试接口，
// 在真实浏览器内核里把关键界面走一遍。零依赖（只用 Node 自带的 fetch / WebSocket）。
//
// 用法（先确保前后端在跑，即 scripts\启动-前后端.bat）：
//     node scripts\浏览器自检.mjs
//
// 它验的是**渲染与交互**——也就是 HTTP 层测不到的那部分：
// 路由守卫、按角色渲染的菜单、无权限提示页、表单联动、后端 422 文案是否原样显示。
// 退出码 0 = 全过。
//
// 注意：C 组会往开发库里写一条自检品类和一条预算（名字带 SELFCHECK 前缀），
// 方便你认出来；重复跑不会冲突（每次的编码都不一样）。

import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const APP = "http://localhost:5173";
const PORT = 9222;
const CDP = "http://127.0.0.1:" + PORT;

const stamp = Date.now().toString(36).toUpperCase();
const CAT_NAME = "自检品类 " + stamp;
const CAT_CODE = "SELFCHECK-" + stamp;

const results = [];
function check(name, ok, detail) {
  results.push(ok);
  console.log((ok ? "PASS  " : "FAIL  ") + name + (detail ? "   —— " + detail : ""));
}

function findEdge() {
  const candidates = [
    "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    "C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    join(process.env.LOCALAPPDATA || "", "Microsoft/Edge/Application/msedge.exe"),
  ];
  return candidates.find((p) => p && existsSync(p));
}

async function ensureAppUp() {
  try {
    const res = await fetch(APP + "/", { signal: AbortSignal.timeout(4000) });
    return res.ok;
  } catch {
    return false;
  }
}

function startBrowser(edgePath, profile) {
  return spawn(
    edgePath,
    [
      "--headless=new",
      "--disable-gpu",
      "--remote-debugging-port=" + PORT,
      "--user-data-dir=" + profile,
      "--no-first-run",
      "--no-default-browser-check",
      "--window-size=1440,1000",
      "about:blank",
    ],
    { stdio: "ignore" },
  );
}

async function waitForCdp(timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(CDP + "/json/version", { signal: AbortSignal.timeout(1500) });
      if (res.ok) return await res.json();
    } catch {
      // 还没起来
    }
    await new Promise((r) => setTimeout(r, 300));
  }
  throw new Error("Edge 的调试端口没起来（" + PORT + "）");
}

async function connect(wsUrl) {
  const ws = new WebSocket(wsUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve, { once: true });
    ws.addEventListener("error", reject, { once: true });
  });
  let seq = 0;
  const pending = new Map();
  const events = [];
  ws.addEventListener("message", (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const entry = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) entry.reject(new Error(msg.error.message));
      else entry.resolve(msg.result);
    } else if (msg.method) {
      events.push(msg);
    }
  });
  const send = (method, params) => new Promise((resolve, reject) => {
    const id = ++seq;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params: params || {} }));
  });
  return { ws, send, events };
}

async function openPage(url) {
  const res = await fetch(CDP + "/json/new?" + encodeURIComponent(url), { method: "PUT" });
  if (!res.ok) throw new Error("新建标签失败：" + res.status);
  const target = await res.json();
  const conn = await connect(target.webSocketDebuggerUrl);
  await conn.send("Page.enable");
  await conn.send("Runtime.enable");
  await conn.send("Network.enable");
  await conn.send("Log.enable");
  const evaluate = async (expression) => {
    const r = await conn.send("Runtime.evaluate", {
      expression,
      returnByValue: true,
      awaitPromise: true,
    });
    if (r.exceptionDetails) {
      throw new Error("页面内报错：" + (r.exceptionDetails.exception
        ? r.exceptionDetails.exception.description
        : r.exceptionDetails.text));
    }
    return r.result.value;
  };
  return { send: conn.send, evaluate, events: conn.events, close: () => conn.ws.close() };
}

async function waitFor(page, expression, label, timeoutMs = 12000) {
  const deadline = Date.now() + timeoutMs;
  let last;
  while (Date.now() < deadline) {
    // 用 expr() 包一层：这样等待条件里也能用 byLabel() 这些辅助函数
    last = await page.evaluate(expr("return (" + expression + ");"));
    if (last) return true;
    await new Promise((r) => setTimeout(r, 150));
  }
  throw new Error("等待超时：" + label + "（最后结果=" + JSON.stringify(last) + "）");
}

// React 受控组件必须走原生 setter，否则 onChange 收不到
const HELPERS = [
  "function setInput(el,v){",
  "var proto=el.tagName==='SELECT'?HTMLSelectElement.prototype:HTMLInputElement.prototype;",
  "var s=Object.getOwnPropertyDescriptor(proto,'value').set;s.call(el,v);",
  "el.dispatchEvent(new Event(el.tagName==='SELECT'?'change':'input',{bubbles:true}));}",
  "function byLabel(text){var ls=[].slice.call(document.querySelectorAll('label'));",
  "var l=ls.filter(function(x){return x.textContent.trim().indexOf(text)===0})[0];",
  "return l?l.querySelector('input,select'):null;}",
  "function texts(sel){return [].slice.call(document.querySelectorAll(sel)).map(function(e){return e.textContent.trim()})}",
].join("");

const expr = (body) => "(() => {" + HELPERS + body + "})()";

const loginExpr = (u, p) => expr(
  "setInput(document.querySelector('input[autocomplete=\"username\"]')," + JSON.stringify(u) + ");"
  + "setInput(document.querySelector('input[type=\"password\"]')," + JSON.stringify(p) + ");"
  + "document.querySelector('form').requestSubmit();return true;",
);

// M4 起外壳是「左侧边栏 + 右侧内容区」：账号与退出在侧边栏底部，
// 而菜单在同一侧的 <nav data-testid="app-nav"> 里 —— 所以分开取，别混成一个字符串。
const sidebarTextExpr =
  "(document.querySelector('aside')||document.body).innerText.replace(/\\s+/g,' ')";
const menuItemsExpr =
  "[].slice.call(document.querySelectorAll('[data-testid=app-nav] a'))"
  + ".map(function(a){return a.textContent.trim()})";
const bodyTextExpr = "document.body.innerText.replace(/\\s+/g,' ')";

// 品类表单：input 顺序是 [名称, 编码, 阈值…]
const createCategoryExpr = (name, code, threshold) => expr(
  "var f=document.querySelector('form');var ins=f.querySelectorAll('input');"
  + "setInput(ins[0]," + JSON.stringify(name) + ");"
  + "setInput(ins[1]," + JSON.stringify(code) + ");"
  + "setInput(ins[2]," + JSON.stringify(threshold) + ");"
  + "f.querySelector('button[type=submit]').click();return true;",
);

const createBudgetExpr = (categoryName, yuan) => expr(
  "var f=document.querySelector('form');var sels=f.querySelectorAll('select');"
  + "var dep=sels[0].options[1];setInput(sels[0],dep.value);"
  + "var cat=[].slice.call(sels[1].options).filter(function(o){return o.textContent.trim()===" + JSON.stringify(categoryName) + "})[0];"
  + "if(!cat){return 'NO_CATEGORY_OPTION';}"
  + "setInput(sels[1],cat.value);"
  + "setInput(byLabel('预算金额')," + JSON.stringify(yuan) + ");"
  + "f.querySelector('button[type=submit]').click();return 'OK';",
);

async function main() {
  if (!(await ensureAppUp())) {
    console.log("前端没在 " + APP + " 上。先跑：.\\scripts\\启动-前后端.bat");
    return 1;
  }
  const edgePath = findEdge();
  if (!edgePath) {
    console.log("找不到 Edge。这个脚本要用 Edge 的调试接口。");
    return 1;
  }

  const profile = mkdtempSync(join(tmpdir(), "procurement-selfcheck-"));
  const browser = startBrowser(edgePath, profile);

  try {
    const version = await waitForCdp();
    console.log("浏览器：" + version.Browser + "（headless，不影响你自己开着的 Edge）\n");

    const page = await openPage(APP + "/");
    await page.send("Network.clearBrowserCookies");

    // ---------- A. 未登录与登录 ----------
    await page.send("Page.navigate", { url: APP + "/admin/suppliers" });
    await waitFor(page, "location.pathname === '/login'", "匿名访问被弹到登录页");
    const jar = await page.send("Network.getCookies", { urls: [APP] });
    check("A1 未登录访问 /admin/suppliers 跳到 /login，且一个 Cookie 都没有",
      (jar.cookies || []).length === 0, "cookie 数=" + (jar.cookies || []).length);

    // ---------- 密码框：常驻的显示/隐藏开关 ----------
    // 注意：React 的状态更新是异步的，点完不能立刻读 DOM —— 要等它渲染完。
    const toggleBtn = "var btn=[].slice.call(document.querySelectorAll('button'))"
      + ".filter(function(b){return /显示|隐藏/.test(b.textContent)})[0];";
    const before = await page.evaluate(expr(
      toggleBtn + "return {has: !!btn, label: btn?btn.textContent.trim():null,"
      + "type: byLabel('密码')?byLabel('密码').type:null};",
    ));
    await page.evaluate(expr(toggleBtn + "if(btn){btn.click();}return true;"));
    await waitFor(page, "byLabel('密码').type === 'text'", "密码框切成明文", 4000);
    const after = await page.evaluate(expr("return byLabel('密码').type;"));
    check("A3 密码框有常驻的显示/隐藏开关，点击后类型在 password/text 之间切换",
      before.has === true && before.type === "password" && after === "text",
      "按钮=" + before.label + " 点击前=" + before.type + " 点击后=" + after);
    // 切回隐藏，验证它是双向的
    await page.evaluate(expr(toggleBtn + "if(btn){btn.click();}return true;"));
    await waitFor(page, "byLabel('密码').type === 'password'", "密码框切回隐藏", 4000);
    const { writeFileSync } = await import("node:fs");
    const loginShot = await page.send("Page.captureScreenshot", { format: "png" });
    writeFileSync(join(tmpdir(), "procurement-login.png"), Buffer.from(loginShot.data, "base64"));

    await page.evaluate(loginExpr("admin", "Dev-Passw0rd!"));
    await waitFor(page, "location.pathname === '/admin/suppliers'", "登录后回到原页面", 15000);
    check("A2 登录后回到原来想去的 /admin/suppliers（不是首页）", true,
      await page.evaluate("location.pathname"));

    // M4：菜单按角色渲染 —— 断言不能只看"菜单里有没有那几个字"，
    // 还要拿**后端返回的角色**跟侧边栏显示的账号对上，否则样式把账号区藏起来也能蒙混过关。
    const me = await page.evaluate(
      "(async () => (await fetch('/api/v1/auth/me')).json())()",
    );
    const sidebar = await page.evaluate(sidebarTextExpr);
    const adminMenu = await page.evaluate(menuItemsExpr);
    check("A4 侧边栏显示账号与角色，并出现管理员菜单",
      sidebar.includes("系统管理员") && sidebar.includes(me.roles.join("+"))
      && sidebar.includes("退出")
      && adminMenu.indexOf("采购品类") >= 0 && adminMenu.indexOf("预算") >= 0
      && adminMenu.indexOf("供应商管理") >= 0 && adminMenu.indexOf("环境自检") >= 0,
      "账号区=" + sidebar.slice(-40) + " 菜单=" + adminMenu.join("/")
      + " 后端角色=" + me.roles.join("+"));

    const cookie = await page.evaluate("document.cookie");
    check("A5 JS 读不到令牌（document.cookie 为空 = HttpOnly 生效）", cookie === "",
      "document.cookie=" + JSON.stringify(cookie));

    // ---------- C. 三个管理页的真实操作 ----------
    await page.send("Page.navigate", { url: APP + "/admin/categories" });
    await waitFor(page, "!!document.querySelector('form')", "品类页渲染");
    await waitFor(page, "document.body.innerText.indexOf('新增采购品类')>=0", "品类页标题");

    await page.evaluate(createCategoryExpr(CAT_NAME, CAT_CODE, "0"));
    await waitFor(page, "document.body.innerText.indexOf(" + JSON.stringify(CAT_NAME) + ")>=0",
      "新建的品类出现在列表里", 15000);
    check("C1 在界面上新建品类 → 列表立刻出现（TanStack Query 自动刷新）", true, CAT_CODE);

    await page.evaluate(createCategoryExpr("自检-无兜底 " + stamp, "SELFCHECK-BAD-" + stamp, "1000"));
    await waitFor(page, "document.body.innerText.indexOf('空审批链')>=0",
      "后端 422 原文出现在页面上", 15000);
    const errText = await page.evaluate(expr(
      "var p=[].slice.call(document.querySelectorAll('p'))"
      + ".filter(function(x){return x.textContent.indexOf('空审批链')>=0})[0];"
      + "return p?p.textContent.replace(/\\s+/g,' ').slice(0,110):'';",
    ));
    check("C2 没有兜底的策略被拦下，且把后端 422 的原文显示出来", true, errText);

    await page.send("Page.navigate", { url: APP + "/admin/budgets" });
    await waitFor(page, "!!document.querySelector('form')", "预算页渲染");
    await waitFor(page, "document.querySelectorAll('form select').length >= 2", "预算页表单就绪");
    // 品类下拉的选项是异步拉回来的：光等"表单渲染出来"不够 ——
    // 那一刻下拉里只有「请选择」，会在下面被误判成"品类不存在"。
    // 部门下拉的选项来自另一个接口，所以两个都要等到真正有数据。
    await waitFor(
      page,
      "(function(){var s=document.querySelectorAll('form select');"
      + "return s.length>=2 && s[0].options.length>=2 && s[1].options.length>=2;})()",
      "预算页下拉里出现部门与品类",
    );
    const budgetResult = await page.evaluate(createBudgetExpr(CAT_NAME, "1000"));
    if (budgetResult !== "OK") {
      check("C3 新建预算（填 1000 元）→ 列表显示 ¥1,000.00", false,
        "下拉里没找到刚建的品类（" + budgetResult + "）");
    } else {
      await waitFor(page, "document.body.innerText.indexOf('¥1,000.00')>=0",
        "列表出现 ¥1,000.00", 15000);
      check("C3 新建预算（填 1000 元）→ 列表显示 ¥1,000.00（界面元、库里分）", true);
    }

    // ---------- B. 换账号看权限 ----------
    await page.send("Page.navigate", { url: APP + "/admin/suppliers" });
    await waitFor(page, "!!document.querySelector('aside')", "回到管理页（侧边栏已渲染）");
    await page.evaluate(expr(
      "var b=[].slice.call(document.querySelectorAll('button'))"
      + ".filter(function(x){return x.textContent.trim()==='退出'})[0];"
      + "if(b){b.click();}return true;",
    ));
    await waitFor(page, "location.pathname === '/login'", "退出后回登录页");
    check("A6 点「退出」→ 回到 /login", true, await page.evaluate("location.pathname"));

    await waitFor(page, "!!document.querySelector('input[type=password]')", "登录页可用");
    await page.evaluate(loginExpr("requester", "Dev-Passw0rd!"));
    await waitFor(page, "location.pathname === '/'", "requester 登录成功");
    const rqSidebar = await page.evaluate(sidebarTextExpr);
    const rqMenu = await page.evaluate(menuItemsExpr);
    // 4 项 = 环境自检 / 我的申请 / 我的待办 / 合格供应商
    //（M5 加了「我的申请」，M6 加了「我的待办」—— 它不限角色，登录即可见）。
    // 断言写成"恰好这几项"，而不是"数量等于 4"：数量对不上时，
    // 报错信息里能直接看出多了或少了哪一项。
    check("B1 requester 侧边栏没有管理员菜单，只剩普通员工的几项",
      rqSidebar.includes("张申请") && rqSidebar.includes("REQUESTER")
      && rqMenu.join("/") === "环境自检/我的申请/我的待办/合格供应商",
      "菜单=" + rqMenu.join("/"));

    await page.send("Page.navigate", { url: APP + "/admin/categories" });
    await waitFor(page, "document.body.innerText.indexOf('无权限')>=0", "出现无权限提示页");
    const forbidden = await page.evaluate(expr(
      "return " + bodyTextExpr + ".slice(-90);",
    ));
    check("B2 requester 打开 /admin/categories 显示「无权限」而不是白屏", true, forbidden);

    // 顺手留一张截图（只看渲染有没有崩，不参与判定）
    const shot = await page.send("Page.captureScreenshot", { format: "png" });
    writeFileSync(join(tmpdir(), "procurement-selfcheck.png"), Buffer.from(shot.data, "base64"));
    console.log("\n截图已存：" + join(tmpdir(), "procurement-login.png")
      + " 和 " + join(tmpdir(), "procurement-selfcheck.png"));

    // 控制台报错只作参考：浏览器的网络类告警（比如 favicon）也会落到这里，
    // 计入判定会造成假失败，所以只打印不判定。
    const consoleErrors = page.events.filter((e) =>
      (e.method === "Runtime.consoleAPICalled" && e.params.type === "error")
      || (e.method === "Log.entryAdded" && e.params.entry.level === "error"));
    if (consoleErrors.length > 0) {
      console.log("\n浏览器控制台 error（供参考，不计入判定）：");
      for (const e of consoleErrors.slice(0, 5)) {
        const text = e.params.entry ? e.params.entry.text
          : (e.params.args || []).map((a) => a.value || a.description || "").join(" ");
        console.log("  · " + String(text).slice(0, 160));
      }
    } else {
      console.log("\n浏览器控制台没有 error。");
    }
  } finally {
    try {
      const conn = await connect((await (await fetch(CDP + "/json/version")).json()).webSocketDebuggerUrl);
      await conn.send("Browser.close");
      conn.ws.close();
    } catch {
      browser.kill();
    }
    await new Promise((r) => setTimeout(r, 800));
    try { browser.kill(); } catch { /* 已经退出 */ }
    try { rmSync(profile, { recursive: true, force: true }); } catch { /* 文件被占就先留着 */ }
  }

  const failed = results.filter((ok) => !ok).length;
  console.log("\n共 " + results.length + " 项，失败 " + failed + " 项");

  // 本脚本会建 1 个品类（SELFCHECK-*）与 1 条配套预算，而这两样都**没有删除端点**
  // （删数据属于运维动作）。清理统一走一个脚本，与页面实测共用：
  //     cd backend; .venv\Scripts\python.exe scripts\testdata_cleanup.py --scope all
  console.log(
    "\n提示：本次自检建了 1 个品类（" + CAT_CODE + "）与 1 条配套预算。"
    + "\n      清理（连页面实测的一起）：cd backend; "
    + ".venv\\Scripts\\python.exe scripts\\testdata_cleanup.py --scope all",
  );

  return failed === 0 ? 0 : 1;
}

main()
  .then((code) => process.exit(code))
  .catch((err) => {
    console.error("自检中断：" + err.message);
    process.exit(2);
  });

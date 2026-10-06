// 窄屏自检（<768px）：在无窗口 Edge 里把视口缩到手机宽度，检查响应式是否真的生效。
//
// 用法（先确保前后端在跑）：
//     node scripts\窄屏自检.mjs
//
// 为什么单独写一个：`浏览器自检.mjs` 与 `m6-页面实测.mjs` 都跑在桌面宽度（1440）上，
// 而**响应式是纯 class 断点**（`hidden md:flex` / `lg:grid-cols-…` / `md:hidden`），
// 桌面宽度下这些分支根本不会被执行到 —— 也就是说"窄屏能用"在那两个脚本里是**未验证**的。
//
// 它验四件事：
//   1. 375px 下**没有横向溢出**（每个页面都查一遍）；这是手机端最容易翻车的地方；
//   2. 断点两侧的差异**真的发生了**：侧边栏窄屏隐藏 / 宽屏显示，页内面包屑窄屏显示；
//      这就是"响应式生效"的硬证据，比肉眼看截图可靠；
//   3. 关键控件在窄屏仍然可见可点：登录表单、申请列表的选择控件、详情页的导航折叠；
//   4. 顺手留下 375px 的截图（browser-screenshots/），给报告当证据。
//
// 它**不写库**：只用演示账号登录、只读页面。退出码 0 = 全过。

import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const APP = "http://localhost:5173";
const PORT = 9222;
const CDP = "http://127.0.0.1:" + PORT;
const SHOTS = "browser-screenshots";

//: 手机竖屏（iPhone X 的逻辑分辨率），断点 md=768 / lg=1024 都在它上面
const NARROW = { width: 375, height: 812 };
const WIDE = { width: 1280, height: 900 };

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
  ws.addEventListener("message", (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && pending.has(msg.id)) {
      const entry = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) entry.reject(new Error(msg.error.message));
      else entry.resolve(msg.result);
    }
  });
  const send = (method, params) =>
    new Promise((resolve, reject) => {
      const id = ++seq;
      pending.set(id, { resolve, reject });
      ws.send(JSON.stringify({ id, method, params: params || {} }));
    });
  return { ws, send };
}

async function openPage(url) {
  const res = await fetch(CDP + "/json/new?" + encodeURIComponent(url), { method: "PUT" });
  if (!res.ok) throw new Error("新建标签失败：" + res.status);
  const target = await res.json();
  const conn = await connect(target.webSocketDebuggerUrl);
  await conn.send("Page.enable");
  await conn.send("Runtime.enable");
  await conn.send("Network.enable");
  const evaluate = async (expression) => {
    const r = await conn.send("Runtime.evaluate", {
      expression,
      returnByValue: true,
      awaitPromise: true,
    });
    if (r.exceptionDetails) {
      const d = r.exceptionDetails.exception
        ? r.exceptionDetails.exception.description
        : r.exceptionDetails.text;
      throw new Error("页面内报错：" + d);
    }
    return r.result.value;
  };
  return { send: conn.send, evaluate, close: () => conn.ws.close() };
}

// React 受控组件必须走原生 setter，否则 onChange 收不到（同 浏览器自检.mjs）
const HELPERS = [
  "function setInput(el,v){",
  "var proto=el.tagName==='SELECT'?HTMLSelectElement.prototype:HTMLInputElement.prototype;",
  "var s=Object.getOwnPropertyDescriptor(proto,'value').set;s.call(el,v);",
  "el.dispatchEvent(new Event(el.tagName==='SELECT'?'change':'input',{bubbles:true}));}",
  "function byLabel(text){var ls=[].slice.call(document.querySelectorAll('label'));",
  "var l=ls.filter(function(x){return x.textContent.trim().indexOf(text)===0})[0];",
  "return l?l.querySelector('input,select'):null;}",
].join("");

const expr = (body) => "(() => {" + HELPERS + body + "})()";

/** 视口切换：走 CDP 的 Emulation 域，等价于设备模拟（不是"改窗口大小"那么简单）。 */
async function setViewport(page, size) {
  await page.send("Emulation.setDeviceMetricsOverride", {
    width: size.width,
    height: size.height,
    deviceScaleFactor: 1,
    mobile: size.width < 768,
  });
  await new Promise((r) => setTimeout(r, 350)); // 等 CSS 断点重新计算 + 布局稳定
}

/** 横向溢出探针：找第一个右边缘超出视口的元素，用于定位原因。 */
const overflowProbe = expr(
  "var vw=window.innerWidth;var bad=[];"
  + "var all=document.querySelectorAll('body *');"
  + "for(var i=0;i<all.length;i++){var r=all[i].getBoundingClientRect();"
  + "if(r.width>0&&r.right>vw+1){bad.push(all[i].tagName.toLowerCase()"
  + "+'.'+(all[i].className&&all[i].className.baseVal===undefined?String(all[i].className).slice(0,60):'')"
  + "+' right='+Math.round(r.right));}}"
  + "return {vw:vw,scrollWidth:document.documentElement.scrollWidth,"
  + "bodyScrollWidth:document.body.scrollWidth,offenders:bad.slice(0,5)};",
);

async function capture(page, name) {
  mkdirSync(SHOTS, { recursive: true });
  const path = join(SHOTS, name);
  const r = await page.send("Page.captureScreenshot", { format: "png" });
  writeFileSync(path, Buffer.from(r.data, "base64"));
  return path;
}

/** 断点差异断言用：某元素在当前视口下是不是"可见"（有盒子、没被 display:none 掉）。 */
const visibleExpr = (selector) =>
  expr(
    "var el=document.querySelector(" + JSON.stringify(selector) + ");"
    + "if(!el)return null;"
    + "var cs=getComputedStyle(el);var r=el.getBoundingClientRect();"
    + "return {display:cs.display,visible:cs.display!=='none'&&r.width>0&&r.height>0,"
    + "width:Math.round(r.width),height:Math.round(r.height)};",
  );

async function main() {
  if (!(await ensureAppUp())) {
    console.log("前端没在 " + APP + " 上。先跑：.\\scripts\\start.ps1");
    return 1;
  }
  const edgePath = findEdge();
  if (!edgePath) {
    console.log("找不到 Edge。这个脚本要用 Edge 的调试接口。");
    return 1;
  }

  const profile = mkdtempSync(join(tmpdir(), "procurement-narrow-"));
  const browser = startBrowser(edgePath, profile);

  try {
    const version = await waitForCdp();
    console.log("浏览器：" + version.Browser + "（headless）");
    console.log("窄屏视口：" + NARROW.width + "×" + NARROW.height + "\n");

    const page = await openPage(APP + "/login");
    await page.send("Network.clearBrowserCookies");

    // ---------- A. 登录页 ----------
    await page.send("Page.navigate", { url: APP + "/login" });
    await setViewport(page, NARROW);
    await new Promise((r) => setTimeout(r, 600));

    const loginOverflow = await page.evaluate(overflowProbe);
    check(
      "A1 登录页无横向溢出（375px）",
      loginOverflow.scrollWidth <= loginOverflow.vw,
      "scrollWidth=" + loginOverflow.scrollWidth + " vs vw=" + loginOverflow.vw
        + (loginOverflow.offenders.length ? " 越界元素=" + loginOverflow.offenders.join(" | ") : ""),
    );

    const formBox = await page.evaluate(visibleExpr("form"));
    check(
      "A2 登录表单在窄屏可见且未超出视口",
      !!formBox && formBox.visible && formBox.width <= NARROW.width,
      JSON.stringify(formBox),
    );

    // 登录页是**独立全屏**布局：外壳（侧边栏 + 顶栏）完全不参与渲染。
    // 这里断言"外层没有主内容区 `<main>`，侧边栏也不可见" ——
    // 而不是"aside 不存在"：Tailwind 的 `md:flex` 让它在窄屏是 display:none，
    // 它可能仍在 DOM 里（那是外壳的骨架，不是页面内容）。
    const loginShell = await page.evaluate(visibleExpr("main"));
    const loginAside = await page.evaluate(visibleExpr("aside"));
    check(
      "A3 登录页不套外壳：没有可见的主内容区，侧边栏也不可见",
      (loginShell === null || loginShell.visible === false)
        && (loginAside === null || loginAside.visible === false),
      "main=" + JSON.stringify(loginShell) + " aside=" + JSON.stringify(loginAside),
    );

    check(
      "A4 登录页窄屏截图已留存",
      existsSync(await capture(page, "窄屏-登录页-375.png")),
      SHOTS + "/窄屏-登录页-375.png",
    );

    // ---------- B. 登录后：断点差异真的发生了 ----------
    // 用 `dept-manager`（部门主管）：他既有部门（能看到「我的申请」），
    // 又不是管理员 —— 菜单是"菜单项最少"的那一档，窄屏折行最容易被挤坏。
    // 演示口令从登录页的说明里读，脚本里不写死密码（改了种子数据也不用改这里）
    const ACCOUNT = "dept-manager";
    const creds = await page.evaluate(expr(
      "var d=document.querySelector('details');if(d)d.open=true;"
      + "var t=document.body.innerText;"
      + "var m=t.match(/口令统一为\\s*([^\\s（(]+)/);"
      + "return {password:m?m[1]:null};",
    ));
    if (!creds.password) {
      check("B0 能从登录页读到演示口令", false, "没匹配到「口令统一为 …」");
      return 1;
    }

    await page.evaluate(expr(
      "setInput(document.querySelector('input[autocomplete=\"username\"]')," + JSON.stringify(ACCOUNT) + ");"
      + "setInput(document.querySelector('input[type=\"password\"]')," + JSON.stringify(creds.password) + ");"
      + "document.querySelector('form').requestSubmit();return true;",
    ));
    const loggedIn = await (async () => {
      const deadline = Date.now() + 12000;
      while (Date.now() < deadline) {
        if (await page.evaluate("location.pathname !== '/login'")) return true;
        await new Promise((r) => setTimeout(r, 200));
      }
      return false;
    })();
    check("B1 演示账号 " + ACCOUNT + " 登录成功", loggedIn, "pathname=" + (await page.evaluate("location.pathname")));
    if (!loggedIn) return 1;

    await setViewport(page, NARROW);
    await new Promise((r) => setTimeout(r, 500));
    const shellNarrow = {
      aside: await page.evaluate(visibleExpr("aside")),
      crumb: await page.evaluate(visibleExpr("main [aria-label=\"面包屑\"]")),
      nav: await page.evaluate(visibleExpr("[data-testid=app-nav]")),
      navLinks: await page.evaluate(
        "[].slice.call(document.querySelectorAll('[data-testid=app-nav] a'))"
          + ".map(function(a){return a.textContent.trim()}).join('/')",
      ),
    };
    const shellOverflow = await page.evaluate(overflowProbe);
    check(
      "B2 工作台窄屏无横向溢出",
      shellOverflow.scrollWidth <= shellOverflow.vw,
      "scrollWidth=" + shellOverflow.scrollWidth + " vs vw=" + shellOverflow.vw
        + (shellOverflow.offenders.length ? " 越界元素=" + shellOverflow.offenders.join(" | ") : ""),
    );
    // 断点生效的硬证据：同一个侧边栏在窄屏是"页顶的整宽横条"（w-full），
    // 在宽屏是"256px 的固定左栏"（md:w-64）。宽度差异是脚本能客观量出来的。
    check(
      "B3 窄屏下侧边栏折叠成页顶整宽横条（宽度 ≈ 视口宽）",
      !!shellNarrow.aside && shellNarrow.aside.visible
        && Math.abs(shellNarrow.aside.width - NARROW.width) <= 4,
      JSON.stringify(shellNarrow.aside),
    );
    // 断言的是**恰好这几项**，不是"数量等于 N"：数量对不上时报错信息里
    // 能直接看出多了或少了哪一项（B1 那条自检就是这么写的，两边保持同一种写法）。
    //
    // 部门主管（dept-manager）在 M11 之后是这 5 项：
    //   环境自检 / 我的申请 / 我的待办 / 合格供应商 / 制度助手
    // 而**没有**「履约工作台」与「预算看板」——
    // 他没有 PROCUREMENT/RECEIVER/FINANCE 中的任何一个，也不是 FINANCE/ADMIN。
    // 这条断言因此在守两件事：菜单项齐全，以及"不该给的没给"。
    check(
      "B4 窄屏下菜单链接着色齐全（折叠不等于丢功能）",
      shellNarrow.navLinks === "环境自检/我的申请/我的待办/合格供应商/制度助手",
      "菜单=" + shellNarrow.navLinks,
    );
    check(
      "B5 窄屏下页内面包屑可见（顶栏面包屑被 md:hidden 藏掉，不能两处都藏）",
      !!shellNarrow.crumb && shellNarrow.crumb.visible === true,
      "crumb=" + JSON.stringify(shellNarrow.crumb),
    );
    check(
      "B6 工作台窄屏截图已留存",
      existsSync(await capture(page, "窄屏-工作台-375.png")),
      SHOTS + "/窄屏-工作台-375.png",
    );

    // 宽屏对照：同一个元素必须变成"固定宽度左栏" —— 这才说明是断点而不是写死
    await setViewport(page, WIDE);
    const asideWide = await page.evaluate(visibleExpr("aside"));
    const crumbWide = await page.evaluate(visibleExpr("main [aria-label=\"面包屑\"]"));
    check(
      "B7 宽屏（1280px）下侧边栏变为固定左栏（宽度 ≈ 256px）",
      !!asideWide && asideWide.visible && Math.abs(asideWide.width - 256) <= 2,
      JSON.stringify(asideWide),
    );
    check(
      "B8 宽屏下改为顶栏面包屑（页内面包屑隐藏），与 B5 构成对照",
      crumbWide === null || crumbWide.visible === false,
      "main 内 crumb=" + JSON.stringify(crumbWide),
    );

    // ---------- C. 申请列表与详情 ----------
    await page.send("Page.navigate", { url: APP + "/requests" });
    await setViewport(page, NARROW);
    await new Promise((r) => setTimeout(r, 900));

    const listOverflow = await page.evaluate(overflowProbe);
    check(
      "C1 申请列表窄屏无横向溢出",
      listOverflow.scrollWidth <= listOverflow.vw,
      "scrollWidth=" + listOverflow.scrollWidth + " vs vw=" + listOverflow.vw
        + (listOverflow.offenders.length ? " 越界元素=" + listOverflow.offenders.join(" | ") : ""),
    );
    const listText = await page.evaluate("document.body.innerText.replace(/\\s+/g,' ')");
    check(
      "C2 申请列表在窄屏渲染出了内容（不是空白页）",
      listText.indexOf("申请") >= 0,
      "正文长度=" + listText.length,
    );
    check(
      "C3 申请列表窄屏截图已留存",
      existsSync(await capture(page, "窄屏-申请列表-375.png")),
      SHOTS + "/窄屏-申请列表-375.png",
    );

    // 进申请详情：**不靠点列表**（列表可能为空 —— 可见范围是按角色算的），
    // 直接问接口要一条当前账号看得见的申请 id，再直接导航过去。
    const detailId = await page.evaluate(
      "(async () => { const r = await fetch('/api/v1/requests?limit=1');"
      + "if (!r.ok) return null; const j = await r.json();"
      + "return j.items && j.items[0] ? j.items[0].id : null; })()",
    );
    check(
      "C4 当前账号能取到一条可见的申请（详情页才有得验）",
      typeof detailId === "string" && detailId.length === 36,
      "id=" + detailId,
    );
    if (detailId) {
      await page.send("Page.navigate", { url: APP + "/requests/" + detailId });
      await setViewport(page, NARROW);
      await new Promise((r) => setTimeout(r, 1000));
      const detailOverflow = await page.evaluate(overflowProbe);
      const tocNarrow = await page.evaluate(visibleExpr("[data-testid=page-toc]"));
      const detailText = await page.evaluate("document.body.innerText.replace(/\\s+/g,' ')");
      check(
        "C5 申请详情窄屏无横向溢出",
        detailOverflow.scrollWidth <= detailOverflow.vw,
        "path=" + (await page.evaluate("location.pathname"))
          + " scrollWidth=" + detailOverflow.scrollWidth + " vs vw=" + detailOverflow.vw
          + (detailOverflow.offenders.length ? " 越界元素=" + detailOverflow.offenders.join(" | ") : ""),
      );
      check(
        "C6 详情页右侧「本页导航」在窄屏隐藏（lg 断点），且正文仍在",
        (tocNarrow === null || tocNarrow.visible === false) && detailText.length > 20,
        "toc=" + JSON.stringify(tocNarrow) + " 正文长度=" + detailText.length,
      );
      check(
        "C7 申请详情窄屏截图已留存",
        existsSync(await capture(page, "窄屏-申请详情-375.png")),
        SHOTS + "/窄屏-申请详情-375.png",
      );

      await setViewport(page, WIDE);
      const tocWide = await page.evaluate(visibleExpr("[data-testid=page-toc]"));
      check(
        "C8 宽屏（1280px）下「本页导航」恢复显示（与 C6 构成对照）",
        !!tocWide && tocWide.visible === true,
        "toc=" + JSON.stringify(tocWide),
      );
    }

    const passed = results.filter(Boolean).length;
    console.log("\n" + passed + "/" + results.length + " 项通过");
    // ⚠️ **必须显式退出**，否则进程不结束（实测：跑完 20/20 之后一直挂着，
    // 每一轮都留一个 node 进程）。
    // 原因有两处，都不是"忘了关"那么简单：
    //   ① 与 CDP 的 WebSocket 是**我们自己 new 的**，`conn.close()` 只关它、
    //      但这个 `ws` 对象没有 unref，事件循环会一直等它；
    //   ② `browser.kill()` 没有 await，而且它返回的句柄也是活的。
    // 结论已经打印完了，这里强制退出不会掩盖任何东西 ——
    // 退出码由 `passed === results.length` 决定，与"是否还有句柄活着"无关。
    process.exit(passed === results.length ? 0 : 1);
  } finally {
    browser.kill();
    try {
      rmSync(profile, { recursive: true, force: true });
    } catch {
      // 删不掉就算了（Windows 上偶发占用）
    }
  }
}

process.exitCode = await main();

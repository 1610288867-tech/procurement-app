/** 应用外壳：查询客户端 + 路由 + 「侧边栏 + 顶栏 + 内容区」（M4 建，样式参考主流后台再刷一遍）。
 *
 * ============================ 布局 ============================
 *   ┌──────────┬──────────────────────────────────────────────┐
 *   │ 品牌      │ 顶栏：面包屑（我在哪） ＋ 环境标记            │
 *   ├──────────┼──────────────────────────────────────────────┤
 *   │ 导航分组  │ 内容区（页面自己排版）                        │
 *   │ …        │                                              │
 *   │ 账号卡片  │                                              │
 *   └──────────┴──────────────────────────────────────────────┘
 *
 * 三条不肯让步的约定：
 *
 * 1. **菜单按角色渲染，但它只是体验优化** —— 后端每个接口都会独立校验
 *    （见 components/RequireAuth.tsx 的说明）。前端隐藏菜单拦不住 Postman。
 * 2. **导航项的集合与顺序不要随手改**：浏览器自检拿 `[data-testid=app-nav]` 里的
 *    `<a>` 文本**逐个对**（requester 那条是"恰好这几项"）。要加菜单，
 *    先想清楚自检那条断言要不要同步改。
 * 3. **登录页不套外壳**：`/login` 是全屏分栏（左品牌介绍 + 右表单），
 *    一屏里同时出现"侧边栏显示已登录"和"中间摆着登录表单"是自相矛盾的。
 *
 * 响应式只做一件事：窄屏时侧边栏折到顶部并横向排开。
 * 不做抽屉、不做汉堡菜单 —— 这个系统的用户是办公场景，桌面为主，
 * 多一层交互状态就多一类 bug，而它换不来实际收益。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Link, NavLink, useLocation, useNavigate } from "react-router-dom";

import { useLogout, useMe } from "./api/hooks/useAuth";
import AppRoutes from "./router";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // 401 不重试（M3 起由登录页接管）
      retry: (failureCount, error) =>
        (error as { status?: number }).status === 401 ? false : failureCount < 2,
      refetchOnWindowFocus: false,
    },
  },
});

const navLinkClass = ({ isActive }: { isActive: boolean }) =>
  "flex items-center gap-2 rounded-md px-3 py-2 text-sm transition-colors " +
  (isActive
    ? "bg-primary font-medium text-primary-fg"
    : "text-muted hover:bg-hover hover:text-ink");

/** 导航图标：16px 线性图标，`currentColor` 跟随文字色。
 *  单独抽出来是为了让菜单项只写一次 `<NavItem icon={…}>`，不要每项各画一遍 SVG。 */
const ICONS: Record<string, string> = {
  pulse: "M3 12h3l2.5-6 3 12L14 12h3",
  doc: "M6 3h7l4 4v10a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Zm7 0v4h4",
  inbox: "M4 13h4l1 2h6l1-2h4M4 13l2-8h12l2 8v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-5Z",
  truck: "M3 7h10v9H3V7Zm10 3h4l3 3v3h-7v-6ZM7 19a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3Zm10 0a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3Z",
  chart: "M4 19V5m0 14h16M8 19v-6m4 6V9m4 10v-4",
  tag: "M4 12 12 4h7v7l-8 8-7-7Zm10-4h.01",
  shield: "M12 4l7 3v5c0 4-3 7-7 8-4-1-7-4-7-8V7l7-3Z",
  wallet: "M4 8h13a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H5a1 1 0 0 1-1-1V8Zm0 0V6a1 1 0 0 1 1-1h11M15 13h.01",
  // M10 履约工作台。**不复用 `inbox`**：那一个已经是「我的待办」的图标，
  // 两个菜单项长一样时用户会靠读数第二遍来区分——图标就白放了。
  box: "M4 8l8-4 8 4v8l-8 4-8-4V8Zm0 0 8 4 8-4M12 12v8",
  // M11 制度助手：一本翻开的书（"查制度"最直白的形状）
  book: "M5 5a2 2 0 0 1 2-2h5v16H7a2 2 0 0 1-2-2V5Zm7-2h5a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-5",
};

function Icon({ name }: { name: keyof typeof ICONS | string }) {
  const path = ICONS[name] ?? ICONS.doc;
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      className="h-4 w-4 shrink-0"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d={path} />
    </svg>
  );
}

function NavItem({
  to,
  end,
  icon,
  children,
}: {
  to: string;
  end?: boolean;
  icon?: string;
  children: string;
}) {
  return (
    <NavLink to={to} end={end} className={navLinkClass}>
      {icon && <Icon name={icon} />}
      {children}
    </NavLink>
  );
}

/** 分组小标题：只占位、不可点（不是 `<a>`，所以自检的菜单项断言不会数到它）。 */
function NavGroup({ children }: { children: string }) {
  return (
    <p className="mt-4 hidden px-3 pb-1 text-xs tracking-wide text-faint md:block">
      {children}
    </p>
  );
}

function Sidebar() {
  const me = useMe();
  const logout = useLogout();
  const navigate = useNavigate();
  const isAdmin = me.data?.roles.includes("ADMIN") ?? false;
  // 能不能发起采购申请，看**有没有部门**，不看角色：
  // 部门决定预算归属与审批链，没有它连草稿都建不出来（后端 422 `NO_DEPARTMENT`）。
  // 判据与路由守卫 `RequireAuth requireDepartment` 必须同源，否则会出现
  // "菜单里有、点进去被拦"或反过来的错配。
  const canRequest = me.isSuccess && Boolean(me.data?.department_id);
  // 预算看板（M7）是**只读**的：财务与管理员的职责里都包含"看钱"，
  // 而改钱仍然只走管理员那条路径。判据与路由守卫的角色列表必须同源。
  const canSeeBudgets =
    me.isSuccess &&
    Boolean(me.data?.roles.some((role) => role === "FINANCE" || role === "ADMIN"));

  // 「履约工作台」（M10）：三个履约角色之一可见。
  // ⚠️ 这条判断与 `router.tsx` 里那条路由守卫的**角色列表必须同源** ——
  // 两处不一致的表现是"菜单上有、点进去说无权限"（或反过来）。
  // 所以两边都写同一组字面量，改的时候一起改。
  const FULFILLMENT_ROLES = ["PROCUREMENT", "RECEIVER", "FINANCE"] as const;
  const canFulfill =
    me.isSuccess &&
    Boolean(me.data?.roles.some((role) => FULFILLMENT_ROLES.includes(role as never)));

  return (
    <aside className="flex w-full shrink-0 flex-col gap-5 border-line bg-section p-4 md:sticky md:top-0 md:h-screen md:w-64 md:border-r md:p-5">
      <Link to="/" className="flex items-center gap-3">
        <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary text-sm font-semibold text-primary-fg">
          采
        </span>
        <span className="min-w-0">
          <span className="block truncate text-sm font-semibold text-ink">
            企业采购审批
          </span>
          <span className="block text-xs text-muted">本地演示环境</span>
        </span>
      </Link>

      <nav data-testid="app-nav" className="flex flex-wrap gap-1 md:flex-col">
        {!me.data && <p className="px-3 text-sm text-muted">未登录</p>}
        {me.data && (
          <>
            <NavItem to="/" end icon="pulse">
              环境自检
            </NavItem>
            {/* 「我的申请」只对有部门的账号显示 —— 没有部门的账号**本来也建不出申请**，
                给他一个点进去只能看空列表、或者被拦下的入口没有意义。 */}
            {canRequest && (
              <NavItem to="/requests" icon="doc">
                我的申请
              </NavItem>
            )}
            {/* 「我的待办」**所有人都保留**：审批与部门无关，
                一个人完全可能既是管理员又是 GM（要审大额单）。 */}
            <NavItem to="/approvals/tasks" icon="inbox">
              我的待办
            </NavItem>
            <NavItem to="/suppliers" icon="truck">
              合格供应商
            </NavItem>
            {/* M11：制度问答。**不限角色** —— 制度是给所有人看的，
                放的位置也说明这一点：它在"我的"系列之后、"工作台"之前。 */}
            <NavItem to="/chat" icon="book">
              制度助手
            </NavItem>
            {/* 「履约工作台」只对三个履约角色显示。判据与路由守卫同源（见上）。
                放在供应商之后、预算看板之前：它是"每天要做的事"，
                比只读的看板更靠前。 */}
            {canFulfill && (
              <NavItem to="/fulfillment" icon="box">
                履约工作台
              </NavItem>
            )}
            {canSeeBudgets && (
              <NavItem to="/budgets" icon="chart">
                预算看板
              </NavItem>
            )}
          </>
        )}
        {isAdmin && (
          <>
            <NavGroup>管理员</NavGroup>
            <NavItem to="/admin/categories" icon="tag">
              采购品类
            </NavItem>
            <NavItem to="/admin/suppliers" icon="shield">
              供应商管理
            </NavItem>
            <NavItem to="/admin/budgets" icon="wallet">
              预算
            </NavItem>
            {/* M11：知识库是"制度依据"的入口 —— 改它等于改所有回答的依据，
                所以只在管理员组里出现（与 `/admin/*` 其余项同一口径）。 */}
            <NavItem to="/admin/knowledge" icon="book">
              知识库
            </NavItem>
          </>
        )}
      </nav>

      <div className="mt-auto border-t border-line pt-4 text-sm">
        {me.data ? (
          <div className="flex items-center gap-3">
            <span
              aria-hidden="true"
              className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-card text-xs font-medium text-body"
            >
              {me.data.full_name.slice(0, 1)}
            </span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-body">{me.data.full_name}</span>
              <span className="block truncate font-mono text-xs text-faint">
                {me.data.roles.join("+")}
              </span>
            </span>
            <button
              type="button"
              className="shrink-0 rounded-md px-2 py-1 text-muted transition-colors hover:bg-hover hover:text-ink disabled:opacity-50"
              disabled={logout.isPending}
              onClick={() =>
                logout.mutate(undefined, {
                  onSuccess: () => navigate("/login", { replace: true }),
                })
              }
            >
              退出
            </button>
          </div>
        ) : (
          <NavLink
            to="/login"
            className="inline-block rounded-md px-3 py-2 text-muted hover:bg-hover hover:text-ink"
          >
            登录
          </NavLink>
        )}
      </div>
    </aside>
  );
}

/** 路由 → 面包屑与页面名。**只写导航层级，不写业务状态。** */
const PAGE_LABELS: { match: RegExp; label: string; parent?: { label: string; to: string } }[] = [
  { match: /^\/$/, label: "环境自检" },
  { match: /^\/requests\/new$/, label: "新建申请", parent: { label: "我的申请", to: "/requests" } },
  { match: /^\/requests\/[^/]+$/, label: "申请详情", parent: { label: "我的申请", to: "/requests" } },
  { match: /^\/requests$/, label: "我的申请" },
  { match: /^\/approvals\/tasks\/[^/]+$/, label: "审批任务", parent: { label: "我的待办", to: "/approvals/tasks" } },
  { match: /^\/approvals\/tasks$/, label: "我的待办" },
  { match: /^\/suppliers$/, label: "合格供应商" },
  { match: /^\/budgets$/, label: "预算看板" },
  // M11：制度问答。**不限角色**（制度是给所有人看的），所以父级是「首页」。
  { match: /^\/chat$/, label: "制度助手" },
  { match: /^\/admin\/categories$/, label: "采购品类", parent: { label: "管理员", to: "/" } },
  { match: /^\/admin\/suppliers$/, label: "供应商管理", parent: { label: "管理员", to: "/" } },
  { match: /^\/admin\/budgets$/, label: "预算", parent: { label: "管理员", to: "/" } },
  { match: /^\/admin\/knowledge$/, label: "知识库", parent: { label: "管理员", to: "/" } },
];

function TopBar() {
  const location = useLocation();
  const current = PAGE_LABELS.find((item) => item.match.test(location.pathname));

  return (
    <div className="sticky top-0 z-10 hidden items-center justify-between gap-4 border-b border-line bg-page/90 px-6 py-3 backdrop-blur md:flex md:px-10">
      <nav aria-label="面包屑" className="flex items-center gap-1.5 text-xs">
        <Link to="/" className="text-muted transition-colors hover:text-ink">
          首页
        </Link>
        {current && current.label !== "环境自检" && (
          <>
            <span className="text-faint">/</span>
            {current.parent ? (
              <>
                <Link
                  to={current.parent.to}
                  className="text-muted transition-colors hover:text-ink"
                >
                  {current.parent.label}
                </Link>
                <span className="text-faint">/</span>
              </>
            ) : null}
            <span className="text-body">{current.label}</span>
          </>
        )}
      </nav>

      <div className="flex items-center gap-3 text-xs text-muted">
        <span className="rounded-sm bg-section px-2 py-0.5">本地演示 · 合成数据</span>
        <a
          href="http://127.0.0.1:8000/docs"
          target="_blank"
          rel="noreferrer"
          className="transition-colors hover:text-ink"
        >
          接口文档 ↗
        </a>
      </div>
    </div>
  );
}

/** 外壳。`/login` 走独立全屏布局（见文件头第 3 条）。 */
function Shell() {
  const location = useLocation();

  if (location.pathname === "/login") {
    return (
      <div className="min-h-screen bg-page">
        <AppRoutes />
      </div>
    );
  }

  return (
    <div className="flex min-h-screen flex-col bg-page md:flex-row">
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar />
        <main className="min-w-0 flex-1 px-6 py-6 md:px-10 md:py-8">
          <div className="mx-auto max-w-5xl space-y-6">
            <AppRoutes />
          </div>
        </main>
      </div>
    </div>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Shell />
      </BrowserRouter>
    </QueryClientProvider>
  );
}

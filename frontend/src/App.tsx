/** 应用外壳：查询客户端 + 路由 + 侧边栏骨架（M4）。
 *
 * ============================ 布局 ============================
 *   左侧固定侧边栏（品牌 / 导航 / 账号）＋ 右侧内容区
 *
 * 菜单**按角色渲染**：这只是体验优化，后端每个接口都会独立校验
 * （见 components/RequireAuth.tsx 的说明）。
 *
 * 响应式只做一件事：窄屏时侧边栏折到顶部并横向排开。
 * 不做抽屉、不做汉堡菜单 —— 这个系统的用户是办公场景，桌面为主，
 * 多一层交互状态就多一类 bug，而它换不来实际收益。
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Link, NavLink, useNavigate } from "react-router-dom";

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
  "block rounded-sm px-3 py-2 text-sm transition-colors " +
  (isActive
    ? "bg-primary font-medium text-primary-fg"
    : "text-muted hover:bg-hover hover:text-ink");

function NavItem({ to, end, children }: { to: string; end?: boolean; children: string }) {
  return (
    <NavLink to={to} end={end} className={navLinkClass}>
      {children}
    </NavLink>
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

  return (
    <aside className="flex w-full shrink-0 flex-col gap-6 border-line bg-section p-4 md:sticky md:top-0 md:h-screen md:w-60 md:border-r md:p-5">
      <div>
        <p className="text-xs tracking-wide text-muted">个人学习项目</p>
        <Link to="/" className="text-base font-semibold text-ink">
          企业采购审批系统
        </Link>
      </div>

      <nav data-testid="app-nav" className="flex flex-wrap gap-1 md:flex-col">
        {!me.data && (
          <p className="px-3 text-sm text-muted">未登录</p>
        )}
        {me.data && (
          <>
            <NavItem to="/" end>
              环境自检
            </NavItem>
            {/* 「我的申请」只对有部门的账号显示 —— 没有部门的账号**本来也建不出申请**，
                给他一个点进去只能看空列表、或者被拦下的入口没有意义。 */}
            {canRequest && <NavItem to="/requests">我的申请</NavItem>}
            {/* 「我的待办」**所有人都保留**：审批与部门无关，
                一个人完全可能既是管理员又是 GM（要审大额单）。 */}
            <NavItem to="/approvals/tasks">我的待办</NavItem>
            <NavItem to="/suppliers">合格供应商</NavItem>
            {canSeeBudgets && <NavItem to="/budgets">预算看板</NavItem>}
          </>
        )}
        {isAdmin && (
          <>
            <p className="mt-3 hidden px-3 text-xs tracking-wide text-faint md:block">
              管理员
            </p>
            <NavItem to="/admin/categories">采购品类</NavItem>
            <NavItem to="/admin/suppliers">供应商管理</NavItem>
            <NavItem to="/admin/budgets">预算</NavItem>
          </>
        )}
      </nav>

      <div className="mt-auto flex items-center justify-between gap-2 border-t border-line pt-4 text-sm">
        {me.data ? (
          <>
            <span className="min-w-0">
              <span className="block truncate text-body">{me.data.full_name}</span>
              <span className="block truncate font-mono text-xs text-faint">
                {me.data.roles.join("+")}
              </span>
            </span>
            <button
              type="button"
              className="shrink-0 text-muted hover:text-ink disabled:opacity-50"
              disabled={logout.isPending}
              onClick={() =>
                logout.mutate(undefined, {
                  onSuccess: () => navigate("/login", { replace: true }),
                })
              }
            >
              退出
            </button>
          </>
        ) : (
          <NavLink to="/login" className="text-muted hover:text-ink">
            登录
          </NavLink>
        )}
      </div>
    </aside>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <div className="flex min-h-screen flex-col bg-page md:flex-row">
          <Sidebar />
          <main className="min-w-0 flex-1 px-6 py-6 md:px-10 md:py-8">
            <div className="mx-auto max-w-5xl space-y-6">
              <AppRoutes />
            </div>
          </main>
        </div>
      </BrowserRouter>
    </QueryClientProvider>
  );
}

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

  return (
    <aside className="flex w-full shrink-0 flex-col gap-6 border-line bg-section p-4 md:sticky md:top-0 md:h-screen md:w-60 md:border-r md:p-5">
      <div>
        <p className="text-xs tracking-wide text-muted">个人学习项目</p>
        <Link to="/" className="text-base font-semibold text-ink">
          企业采购审批系统
        </Link>
      </div>

      <nav data-testid="app-nav" className="flex flex-wrap gap-1 md:flex-col">
        <NavItem to="/" end>
          环境自检
        </NavItem>
        {me.data && <NavItem to="/suppliers">合格供应商</NavItem>}
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

/** 应用外壳：查询客户端 + 路由 + 布局。
 *
 * 页面本身在 src/pages/，路由表在 src/router.tsx。
 * 顶栏显示当前账号并按角色渲染菜单 —— **这只是体验优化**，
 * 后端每个接口都会独立校验。
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
  isActive ? "font-medium text-slate-900" : "text-slate-500 hover:text-slate-800";

function TopBar() {
  const me = useMe();
  const logout = useLogout();
  const navigate = useNavigate();
  const isAdmin = me.data?.roles.includes("ADMIN") ?? false;

  return (
    <>
      <h1 className="text-2xl font-bold text-slate-900">
        <Link to="/">企业采购审批系统</Link>
      </h1>
      <p className="mt-1 text-sm text-slate-500">
        个人学习项目 · 动态多级审批 / 预算并发预留 / 三单匹配
      </p>
      <nav className="mt-3 flex flex-wrap items-center gap-4 text-sm">
        <NavLink to="/" className={navLinkClass} end>
          环境自检
        </NavLink>
        {me.data && (
          <NavLink to="/suppliers" className={navLinkClass}>
            合格供应商
          </NavLink>
        )}
        {isAdmin && (
          <>
            <span className="text-slate-300">|</span>
            <NavLink to="/admin/categories" className={navLinkClass}>
              采购品类
            </NavLink>
            <NavLink to="/admin/suppliers" className={navLinkClass}>
              供应商管理
            </NavLink>
            <NavLink to="/admin/budgets" className={navLinkClass}>
              预算
            </NavLink>
          </>
        )}

        <span className="ml-auto flex items-center gap-3">
          {me.data ? (
            <>
              <span className="text-slate-700">
                {me.data.full_name}
                <span className="ml-1 font-mono text-xs text-slate-400">
                  {me.data.roles.join("+")}
                </span>
              </span>
              <button
                type="button"
                className="text-slate-500 hover:text-slate-800 disabled:opacity-50"
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
            <NavLink to="/login" className={navLinkClass}>
              登录
            </NavLink>
          )}
        </span>
      </nav>
    </>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <main className="min-h-screen bg-slate-50 p-8">
          <div className="mx-auto max-w-3xl space-y-6">
            <header>
              <TopBar />
            </header>

            <AppRoutes />
          </div>
        </main>
      </BrowserRouter>
    </QueryClientProvider>
  );
}

import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";

import { api } from "./api/client";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // 401 不重试（M3 起会有登录页接管）
      retry: (failureCount, error) =>
        (error as { status?: number }).status === 401 ? false : failureCount < 2,
      refetchOnWindowFocus: false,
    },
  },
});

/** M0 的验收页：能打开、能看到后端与数据库的健康状态。
 *
 * 从 M1 起这里会被真正的路由（router.tsx）替换，现在它只用来证明
 * 「前端 → Vite 代理 → FastAPI → PostgreSQL」这条链路是通的。
 */
function HealthPanel() {
  const { data, isPending, isError, error } = useQuery({
    queryKey: ["health", "ready"],
    queryFn: api.ready,
    refetchInterval: 5000,
  });

  return (
    <div className="rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
      <h2 className="mb-3 text-lg font-semibold text-slate-800">环境自检</h2>
      {isPending && <p className="text-slate-500">检查中…</p>}
      {isError && (
        <p className="text-red-600">
          后端不可达：{(error as Error).message}
          <br />
          <span className="text-sm text-slate-500">
            先确认 uvicorn 在 8000 端口跑着，再确认数据库容器是 healthy。
          </span>
        </p>
      )}
      {data && (
        <ul className="space-y-1 text-slate-700">
          <li>
            后端：<span className="font-mono text-green-700">{data.status}</span>
          </li>
          <li>
            数据库：
            <span className="font-mono text-green-700">{data.database}</span>
          </li>
        </ul>
      )}
    </div>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <main className="min-h-screen bg-slate-50 p-8">
        <div className="mx-auto max-w-3xl space-y-6">
          <header>
            <h1 className="text-2xl font-bold text-slate-900">企业采购审批系统</h1>
            <p className="mt-1 text-sm text-slate-500">
              M0 骨架 · 个人学习项目 · 动态多级审批 / 预算并发预留 / 三单匹配
            </p>
          </header>

          <HealthPanel />

          <section className="rounded-lg border border-dashed border-slate-300 p-6 text-sm text-slate-500">
            <p className="font-medium text-slate-700">下一步（M1）</p>
            <p className="mt-1">
              用「供应商」打通 表 → ORM → 仓库 → 协调者 → 路由 → 页面 这条完整链路。
              之后所有功能都是照这个模板扩展。
            </p>
          </section>
        </div>
      </main>
    </QueryClientProvider>
  );
}

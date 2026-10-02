/** 首页：环境自检。
 *
 * M0 用来证明「前端 → Vite 代理 → FastAPI → PostgreSQL」这条链路是通的。
 * M1 起它退居为排查页，业务页面从 /suppliers 开始。
 */

import { useQuery } from "@tanstack/react-query";

import { api } from "../api/client";

export default function Home() {
  const { data, isPending, isError, error } = useQuery({
    queryKey: ["health", "ready"],
    queryFn: api.ready,
    refetchInterval: 5000,
  });

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
      <h2 className="mb-3 text-lg font-semibold text-slate-800">环境自检</h2>

      {isPending && <p className="text-slate-500">检查中…</p>}

      {isError && (
        <div className="text-red-600">
          <p>后端不可达：{(error as Error).message}</p>
          <p className="mt-1 text-sm text-slate-500">
            先确认 uvicorn 在 8000 端口跑着，再确认数据库容器是 healthy。
          </p>
        </div>
      )}

      {data && (
        <ul className="space-y-1 text-slate-700">
          <li>
            后端：<span className="font-mono text-green-700">{data.status}</span>
          </li>
          <li>
            数据库：<span className="font-mono text-green-700">{data.database}</span>
          </li>
        </ul>
      )}
    </section>
  );
}

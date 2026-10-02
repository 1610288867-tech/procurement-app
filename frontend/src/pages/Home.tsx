/** 首页：环境自检。
 *
 * M0 用来证明「前端 → Vite 代理 → FastAPI → PostgreSQL」这条链路是通的。
 * M1 起它退居为排查页，业务页面从 /suppliers 开始。
 * M4 起样式统一走 `components/ui.tsx` 的原语与 `index.css` 的设计令牌。
 */

import { useQuery } from "@tanstack/react-query";

import { api } from "../api/client";
import { Card, Loading, PageHeader } from "../components/ui";

/** 一项检查：左边名称，右边状态值。 */
function CheckItem({ label, value }: { label: string; value: string }) {
  return (
    <li className="flex items-baseline justify-between border-b border-line/60 py-2 last:border-0">
      <span className="text-sm text-muted">{label}</span>
      <span className="font-mono text-sm text-success">{value}</span>
    </li>
  );
}

export default function Home() {
  const { data, isPending, isError, error } = useQuery({
    queryKey: ["health", "ready"],
    queryFn: api.ready,
    refetchInterval: 5000,
  });

  return (
    <>
      <PageHeader
        title="环境自检"
        description="确认前端、后端、数据库三层都在位。这一页每 5 秒自动重查一次。"
      />

      <Card>
        {isPending && <Loading label="检查中…" />}

        {isError && (
          <div className="text-danger">
            <p className="text-sm">后端不可达：{(error as Error).message}</p>
            <p className="mt-1 text-xs text-muted">
              先确认 uvicorn 在 8000 端口跑着，再确认数据库容器是 healthy。
            </p>
          </div>
        )}

        {data && (
          <ul>
            <CheckItem label="后端" value={data.status} />
            <CheckItem label="数据库" value={data.database} />
          </ul>
        )}
      </Card>
    </>
  );
}

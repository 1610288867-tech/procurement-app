/** 首页：工作台 + 环境自检。
 *
 * M0 时这一页只用来证明「前端 → Vite 代理 → FastAPI → PostgreSQL」这条链路是通的；
 * M4 起样式统一走 `components/ui.tsx` 的原语与 `index.css` 的设计令牌。
 * 现在它分两段（参考主流后台的首页：先回答"我该做什么"，再回答"系统是否正常"）：
 *
 *   1. **工作台** —— 问好 + 快捷入口 + 三个数字（我的申请 / 我的待办 / 可见供应商）。
 *      只在**登录后**出现：没登录时这些查询本来就会 401。
 *   2. **环境自检** —— 保留原样（公开可用），它仍然是排查"后端/数据库在不在"的第一站。
 *
 * 一条纪律：这一页**不新开接口**。三个数字分别来自已有的
 * `/requests`、`/approvals/tasks`、`/suppliers`，`total` 字段就是现成的计数。
 */

import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import { useTodoList } from "../api/hooks/useApprovals";
import { useMe } from "../api/hooks/useAuth";
import { useRequests } from "../api/hooks/useRequests";
import { useSuppliers } from "../api/hooks/useCatalog";
import { Card, Loading, PageHeader, Stat } from "../components/ui";

/** 一项检查：左边名称，右边状态值。 */
function CheckItem({ label, value }: { label: string; value: string }) {
  return (
    <li className="flex items-baseline justify-between border-b border-line/60 py-2 last:border-0">
      <span className="text-sm text-muted">{label}</span>
      <span className="font-mono text-sm text-success">{value}</span>
    </li>
  );
}

/** 快捷入口：一个链接 + 一句"点进去干什么"。 */
function Shortcut({
  to,
  title,
  detail,
}: {
  to: string;
  title: string;
  detail: string;
}) {
  return (
    <Link
      to={to}
      className="flex items-start justify-between gap-3 rounded-lg border border-line bg-card px-4 py-3 shadow-card transition-colors hover:border-line-strong hover:bg-section/60"
    >
      <span className="min-w-0">
        <span className="block text-sm font-medium text-ink">{title}</span>
        <span className="mt-0.5 block text-xs text-muted">{detail}</span>
      </span>
      <span aria-hidden="true" className="text-faint">
        →
      </span>
    </Link>
  );
}

function Workbench() {
  const me = useMe();
  const requests = useRequests();
  const todo = useTodoList(1);
  const suppliers = useSuppliers();

  const canRequest = Boolean(me.data?.department_id);
  const isFinanceOrAdmin = Boolean(
    me.data?.roles.some((role) => role === "FINANCE" || role === "ADMIN"),
  );
  const isAdmin = Boolean(me.data?.roles.includes("ADMIN"));
  // M10：履约三个角色之一 —— 与 `App.tsx` 的菜单判断、`router.tsx` 的路由守卫同源。
  const canFulfill = Boolean(
    me.data?.roles.some(
      (role) =>
        role === "PROCUREMENT" || role === "RECEIVER" || role === "FINANCE",
    ),
  );

  // 只取姓名里的"姓 + 名"（后端存的是全名），问好用全名略显生硬
  const name = me.data?.full_name ?? "";

  return (
    <>
      <section className="rounded-lg border border-line bg-card px-6 py-5 shadow-card">
        <h1 className="text-xl font-semibold text-ink">
          你好，{name}
          {todo.data && todo.data.total > 0 && (
            <span className="ml-2 text-muted">
              有 <span className="font-mono text-warning">{todo.data.total}</span> 条待办
            </span>
          )}
        </h1>
        <p className="mt-1 text-sm text-muted">
          {canRequest
            ? "提一笔申请，或者看看现在轮到谁。"
            : "这个账号没有所属部门，发起申请需要先由管理员指定部门。"}
        </p>

        <div className="mt-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {canRequest && (
            <Shortcut
              to="/requests/new"
              title="新建申请"
              detail="选品类、填明细，金额由后端按明细算"
            />
          )}
          <Shortcut
            to="/approvals/tasks"
            title="我的待办"
            detail="只显示现在轮到您的那一级"
          />
          {/* M10：三个履约角色之一才出现。放到"我的待办"之后 ——
              它是**审批通过之后**的下一段活，顺序上自然接在后面。 */}
          {canFulfill && (
            <Shortcut
              to="/fulfillment"
              title="履约工作台"
              detail="询价定标 / 下单收货 / 收票匹配 / 付款归档"
            />
          )}
          {isFinanceOrAdmin ? (
            <Shortcut
              to="/budgets"
              title="预算看板"
              detail="总额 / 已预留 / 已花 / 可用与流水"
            />
          ) : (
            <Shortcut
              to="/suppliers"
              title="合格供应商"
              detail="未停用、且资质未过期"
            />
          )}
          {isAdmin && (
            <Shortcut
              to="/admin/budgets"
              title="配置预算"
              detail="没配预算的部门 × 品类，提交时会被 422 拦住"
            />
          )}
        </div>
      </section>

      <div className="grid gap-4 sm:grid-cols-3">
        <Stat
          label={canRequest ? "我能看到的申请" : "全部申请"}
          value={requests.data ? requests.data.total : "—"}
          hint="按你的可见范围统计（越权一律看不到）"
          to="/requests"
        />
        <Stat
          label="我的待办"
          value={todo.data ? todo.data.total : "—"}
          hint="当前这一级、且不是自己提的"
          tone={todo.data && todo.data.total > 0 ? "warning" : "default"}
          to="/approvals/tasks"
        />
        <Stat
          label="合格供应商"
          value={suppliers.data ? suppliers.data.length : "—"}
          hint="过滤条件写死在后端，客户端改不了"
          to="/suppliers"
        />
      </div>
    </>
  );
}

export default function Home() {
  const me = useMe();
  const { data, isPending, isError, error } = useQuery({
    queryKey: ["health", "ready"],
    queryFn: api.ready,
    refetchInterval: 5000,
  });

  return (
    <>
      <PageHeader
        title={me.data ? "工作台" : "环境自检"}
        description={
          me.data
            ? "先看要做什么，再看系统是否正常。"
            : "确认前端、后端、数据库三层都在位。"
        }
        // 首页也带面包屑：窄屏下顶栏面包屑是 `md:hidden`，页内这一处是**唯一的**
        // 位置提示。首页不给的话，手机上就没有"我在哪"这层信息了。
        breadcrumb={[{ label: "首页" }]}
      />

      {me.data && <Workbench />}

      <Card
        title="环境自检"
        description="这一页每 5 秒自动重查一次；后端或数据库不可用时这里会先红。"
      >
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

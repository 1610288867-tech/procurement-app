/** 我的待办（M6）。
 *
 * ============================ 这一页最要紧的一件事 ============================
 * 列表里**只有"当前这一级"**的任务 —— 那是后端 `approval_flows.current_step_order`
 * 过滤出来的，不是前端藏起来的。
 *
 * 为什么强调这一点：一次提交会把审批链上**每一级**的任务都建出来（这样进度能一眼看全），
 * 如果待办查询只按 `status = 'PENDING'` 过滤，第 4 级的财务在自己还没轮到时就看到了待办，
 * 而且点了"通过"真的会生效 —— 申请可能跳过下面几级被批掉。
 * 前端这里不做任何过滤，也不做乐观更新：审批是"别人也可能同时在处理"的场景。
 */

import { Link, useNavigate } from "react-router-dom";

import { roleLabel } from "../../components/ApprovalTimeline";
import {
  Card,
  Cell,
  EmptyState,
  Loading,
  ErrorNote,
  PageHeader,
  Row,
  Table,
} from "../../components/ui";
import { useTodoList } from "../../api/hooks/useApprovals";
import { formatFen } from "../../lib/money";

function when(value: string | null): string {
  if (!value) return "—";
  return value.slice(0, 16).replace("T", " ");
}

export default function TaskList() {
  const navigate = useNavigate();
  const todo = useTodoList();
  const items = todo.data?.items ?? [];

  return (
    <>
      <PageHeader
        title="我的待办"
        description="只显示现在轮到您的审批。前面几级批完，后面几级才会出现在对应审批人的待办里。"
        breadcrumb={[{ label: "首页", to: "/" }, { label: "我的待办" }]}
      />

      <Card title={`待处理（${todo.data?.total ?? 0}）`}>
        <ErrorNote error={todo.error} />
        {todo.isPending && <Loading label="加载待办…" />}

        {todo.data && items.length === 0 && (
          <EmptyState>现在没有需要您处理的申请。</EmptyState>
        )}

        {items.length > 0 && (
          <Table head={["单号", "申请", "品类", "金额", "我的环节", "提交时间", ""]}>
            {items.map(({ task, request }) => (
              <Row key={task.id}>
                <Cell className="font-mono text-xs text-muted">{request.code}</Cell>
                <Cell>
                  <Link
                    to={`/approvals/tasks/${task.id}`}
                    className="text-body hover:underline"
                  >
                    {request.title}
                  </Link>
                  {request.urgency === "URGENT" && (
                    <span className="ml-2 rounded-sm bg-danger-bg px-1.5 py-0.5 text-xs text-danger">
                      加急
                    </span>
                  )}
                </Cell>
                <Cell>{request.category_name}</Cell>
                <Cell className="whitespace-nowrap text-right font-mono">
                  {formatFen(request.amount_total_fen)}
                </Cell>
                {/* "我的环节"分两行：主信息一行，附注一行 ——
                    挤在一行时这一列会把「处理」列压到折行（实测截图里就是这个问题）。 */}
                <Cell className="whitespace-nowrap">
                  <span className="block">
                    第 {task.step_order} 级 · {roleLabel(task.role)}
                  </span>
                  {(task.mode === "ALL" || task.assignee_id === null) && (
                    <span className="block text-xs text-faint">
                      {task.mode === "ALL" ? "会签" : "或签"}
                      {task.assignee_id === null ? " · 未认领" : ""}
                    </span>
                  )}
                </Cell>
                <Cell className="whitespace-nowrap text-xs text-muted">
                  {when(request.submitted_at)}
                </Cell>
                <Cell className="w-px whitespace-nowrap text-right">
                  <button
                    type="button"
                    className="rounded-sm border border-line-strong px-2 py-1 text-xs text-body hover:border-primary hover:text-ink"
                    onClick={() => navigate(`/approvals/tasks/${task.id}`)}
                  >
                    处理
                  </button>
                </Cell>
              </Row>
            ))}
          </Table>
        )}

        <p className="mt-4 text-xs text-faint">
          列表由后端一次 JOIN 查出金额、申请人与品类，不是"查出任务再逐条查申请"。
        </p>
      </Card>
    </>
  );
}

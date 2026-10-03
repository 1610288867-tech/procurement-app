/** 审批链时间轴（M6）。
 *
 * 把一条审批链画成"已完成 ✓ / 当前 ● / 未开始 ○"三态时间轴，
 * 让申请人一眼看到**自己卡在谁那里**，也让审批人看到前面谁批过了、后面还有几级。
 *
 * ============================ 三态是算出来的，不是另一个状态 ============================
 * 后端 `approval_tasks.status` 里**只有** PENDING / APPROVED / REJECTED / SKIPPED / CANCELLED
 * —— 刻意没有"还没轮到"这一档，因为同一个概念存两处（流说第 3 级、任务说自己在等）
 * 必然漂移。所以这里用**两个输入**算出来：
 *
 *   1. 该级的任务状态（有没有结论）；
 *   2. `currentStepOrder`（现在轮到第几级）。
 *
 * 一个 `step_order` 上可能有**多条**任务（会签 = 该角色下每个人一条），所以先按
 * `step_order` 归并，再判断整级的状态：会签要等全部通过才算完成。
 */

import type { ApprovalRole, ApprovalTask } from "../api/client";
import { TaskStatusBadge } from "./ui";

const ROLE_LABEL: Record<string, string> = {
  DEPT_MANAGER: "部门主管",
  DEPT_HEAD: "部门负责人",
  PROCUREMENT: "采购专员",
  FINANCE: "财务",
  GM: "总经理",
};

export function roleLabel(role: ApprovalRole | string): string {
  return ROLE_LABEL[role] ?? role;
}

type StepState = "DONE" | "REJECTED" | "CURRENT" | "WAITING" | "CANCELLED";

const STEP_MARK: Record<StepState, { icon: string; className: string; label: string }> = {
  DONE: { icon: "✓", className: "bg-success text-white", label: "已完成" },
  REJECTED: { icon: "✕", className: "bg-danger text-white", label: "已驳回" },
  CURRENT: { icon: "●", className: "bg-warning text-white", label: "当前" },
  WAITING: { icon: "○", className: "bg-section text-faint", label: "未开始" },
  CANCELLED: { icon: "—", className: "bg-section text-faint", label: "已取消" },
};

/** 把同一级的任务归并成一个状态。 */
function stepState(tasks: ApprovalTask[], currentStepOrder: number): StepState {
  if (tasks.some((task) => task.status === "REJECTED")) return "REJECTED";
  // 会签：**全部**通过才算这一级完成（或签只有一条，语义一致）
  if (tasks.every((task) => task.status === "APPROVED")) return "DONE";
  if (tasks.every((task) => task.status === "CANCELLED")) return "CANCELLED";
  if (tasks.some((task) => task.status === "SKIPPED")) return "CANCELLED";
  const order = tasks[0]?.step_order;
  return order === currentStepOrder ? "CURRENT" : "WAITING";
}

export function ApprovalTimeline({
  steps,
  currentStepOrder,
  /** 当前用户的任务可以点进去处理（列表页用） */
  onOpenTask,
}: {
  steps: ApprovalTask[];
  currentStepOrder: number;
  onOpenTask?: (taskId: string) => void;
}) {
  if (steps.length === 0) {
    return <p className="text-sm text-muted">还没有审批链（申请尚未提交）。</p>;
  }

  // 按 step_order 归并；顺序取后端给的顺序（后端已按 step_order 排好）
  const byOrder = new Map<number, ApprovalTask[]>();
  for (const task of steps) {
    const bucket = byOrder.get(task.step_order) ?? [];
    bucket.push(task);
    byOrder.set(task.step_order, bucket);
  }
  const orders = [...byOrder.keys()].sort((a, b) => a - b);

  return (
    <ol className="space-y-0" data-testid="approval-timeline">
      {orders.map((order, index) => {
        const tasks = byOrder.get(order) as ApprovalTask[];
        const state = stepState(tasks, currentStepOrder);
        const mark = STEP_MARK[state];
        const first = tasks[0];
        const decided = tasks.filter((task) => task.decided_at !== null);
        return (
          <li key={order} className="flex gap-3">
            {/* 左侧：圆点 + 连接线。线在最后一项收起 */}
            <div className="flex flex-col items-center">
              <span
                className={
                  "flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-xs " +
                  mark.className
                }
                title={mark.label}
                aria-label={mark.label}
              >
                {mark.icon}
              </span>
              {index < orders.length - 1 && (
                <span className="w-px flex-1 bg-line" aria-hidden="true" />
              )}
            </div>

            <div className="flex-1 pb-5">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-sm font-medium text-ink">
                  第 {order} 级 · {roleLabel(first?.role ?? "")}
                </span>
                {first?.mode === "ALL" && (
                  <span className="rounded-sm bg-section px-2 py-0.5 text-xs text-muted">
                    会签 {tasks.length} 人
                  </span>
                )}
                {tasks.map((task) => (
                  <TaskStatusBadge key={task.id} status={task.status} />
                ))}
                {onOpenTask && state === "CURRENT" && (
                  <button
                    type="button"
                    className="text-xs text-primary underline underline-offset-2"
                    onClick={() => onOpenTask(first?.id as string)}
                  >
                    去处理
                  </button>
                )}
              </div>

              {decided.length > 0 && (
                <ul className="mt-1 space-y-0.5">
                  {decided.map((task) => (
                    <li key={task.id} className="text-xs text-muted">
                      {task.status === "APPROVED" ? "通过" : "驳回"}
                      {task.comment ? `：${task.comment}` : "（无意见）"}
                      {task.decided_at ? ` · ${task.decided_at.slice(0, 16).replace("T", " ")}` : ""}
                    </li>
                  ))}
                </ul>
              )}

              {state === "WAITING" && (
                <p className="mt-1 text-xs text-faint">
                  还没轮到这一级 —— 前一级完成后才出现在对应审批人的待办里。
                </p>
              )}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

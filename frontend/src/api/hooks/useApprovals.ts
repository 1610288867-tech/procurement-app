/** 审批的查询与决定 hooks（M6）。
 *
 * 三条与后端对齐的约定：
 *
 * 1. **决定（通过/驳回/认领）都要带 `version`** —— 后端用条件更新（版本进 WHERE）。
 *    两个人同时点"通过"，只有一个成功，另一个 409。前端拿到 409 应当提示"已刷新"，
 *    而不是假装成功。
 *
 * 2. **决定后要同时失效"待办"与"申请详情"**：一次通过会推进审批链（待办变），
 *    也可能把主单推到 SOURCING（申请详情变）。
 *
 * 3. **不做乐观更新**：审批是"别人也可能同时在做"的场景，猜错比慢一点更糟。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "../client";

const TODO = ["approvals", "todo"] as const;
const TASK = ["approvals", "task"] as const;
const FLOW = ["approvals", "flow"] as const;

/** 我的待办。后端**只返回当前这一级**：还没轮到的高级别不会出现。 */
export function useTodoList(limit = 20, offset = 0) {
  return useQuery({
    queryKey: [...TODO, { limit, offset }],
    queryFn: () => api.listTodo({ limit, offset }),
  });
}

export function useTaskDetail(id: string | null) {
  return useQuery({
    queryKey: [...TASK, id ?? "none"],
    queryFn: () => api.getTask(id as string),
    enabled: id !== null,
  });
}

/** 审批链进度（申请人也看得到：自己卡在谁那里）。 */
export function useRequestFlow(requestId: string | null) {
  return useQuery({
    queryKey: [...FLOW, requestId ?? "none"],
    queryFn: () => api.getRequestFlow(requestId as string),
    enabled: requestId !== null,
    // 没有审批流时后端返回 404（还没提交的草稿就是这种状态），
    // 不重试：重试只会在控制台刷一串必然失败的请求。
    retry: false,
  });
}

/** 决定之后统一刷新：待办、任务详情、审批链、申请详情都可能变。 */
function useInvalidateApprovals() {
  const client = useQueryClient();
  return (taskId?: string, requestId?: string) => {
    client.invalidateQueries({ queryKey: TODO });
    if (taskId) client.invalidateQueries({ queryKey: [...TASK, taskId] });
    client.invalidateQueries({ queryKey: FLOW });
    if (requestId) client.invalidateQueries({ queryKey: ["requests", requestId] });
    client.invalidateQueries({ queryKey: ["requests"] });
  };
}

export function useClaimTask() {
  const invalidate = useInvalidateApprovals();
  return useMutation({
    mutationFn: ({ id, version }: { id: string; version: number; requestId?: string }) =>
      api.claimTask(id, version),
    onSuccess: (task) => invalidate(task.id, task.request_id),
  });
}

export function useApproveTask() {
  const invalidate = useInvalidateApprovals();
  return useMutation({
    mutationFn: ({
      id,
      version,
      comment,
    }: {
      id: string;
      version: number;
      comment?: string | null;
      requestId?: string;
    }) => api.approveTask(id, version, comment),
    onSuccess: (_result, variables) => invalidate(variables.id, variables.requestId),
  });
}

export function useRejectTask() {
  const invalidate = useInvalidateApprovals();
  return useMutation({
    mutationFn: ({
      id,
      version,
      comment,
    }: {
      id: string;
      version: number;
      comment: string;
      requestId?: string;
    }) => api.rejectTask(id, version, comment),
    onSuccess: (_result, variables) => invalidate(variables.id, variables.requestId),
  });
}

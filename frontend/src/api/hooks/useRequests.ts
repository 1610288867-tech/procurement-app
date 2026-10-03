/** 采购申请的查询与写操作 hooks（M5）。
 *
 * 写操作成功后 `invalidateQueries` 会自动重拉列表 —— 页面里不用手写"刷新列表"。
 *
 * 一条与后端约定对齐的地方：**提交 / 撤回要带幂等键**。
 * 幂等键由 `lib/idempotency.ts` 生成（同一把 key 复用，重试不会被当成新请求）。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  api,
  type ItemInput,
  type RequestStatus,
  type RequestUrgency,
} from "../client";

const REQUESTS = ["requests"] as const;

const requestKey = (id: string) => ["requests", id] as const;

export function useRequests(status?: RequestStatus) {
  return useQuery({
    queryKey: [...REQUESTS, { status: status ?? null }],
    queryFn: () => api.listRequests({ status }),
  });
}

export function useRequestDetail(id: string | null) {
  return useQuery({
    queryKey: requestKey(id ?? "none"),
    queryFn: () => api.getRequest(id as string),
    // 没有 id 时不查（新建页在还没建草稿时就是这种状态）
    enabled: id !== null,
  });
}

/** 让列表与详情一起失效：任何写操作都会同时影响这两处。 */
function useInvalidateRequests() {
  const client = useQueryClient();
  return (id?: string) => {
    client.invalidateQueries({ queryKey: REQUESTS });
    if (id) client.invalidateQueries({ queryKey: requestKey(id) });
  };
}

export function useCreateRequest() {
  const invalidate = useInvalidateRequests();
  return useMutation({
    mutationFn: api.createRequest,
    onSuccess: (created) => invalidate(created.id),
  });
}

export function usePatchRequest() {
  const invalidate = useInvalidateRequests();
  return useMutation({
    mutationFn: ({
      id,
      ...body
    }: {
      id: string;
      version: number;
      title?: string;
      reason?: string | null;
      urgency?: RequestUrgency;
      form_data?: Record<string, unknown>;
    }) => api.patchRequest(id, body),
    onSuccess: (updated) => invalidate(updated.id),
  });
}

export function useReplaceItems() {
  const invalidate = useInvalidateRequests();
  return useMutation({
    mutationFn: ({
      id,
      version,
      items,
    }: {
      id: string;
      version: number;
      items: ItemInput[];
    }) => api.replaceItems(id, version, items),
    onSuccess: (updated) => invalidate(updated.id),
  });
}

export function useSubmitRequest() {
  const invalidate = useInvalidateRequests();
  return useMutation({
    // 幂等键由**调用方**传入：一次用户动作生成一把，
    // 网络超时后的重试复用同一把 —— 这样服务端才认得出"这是同一件事"。
    // 在这里现生成的话，每次重试都是新键，幂等就名存实亡了。
    mutationFn: ({
      id,
      version,
      idempotencyKey,
    }: {
      id: string;
      version: number;
      idempotencyKey: string;
    }) => api.submitRequest(id, version, idempotencyKey),
    onSuccess: (updated) => invalidate(updated.id),
  });
}

export function useWithdrawRequest() {
  const invalidate = useInvalidateRequests();
  return useMutation({
    mutationFn: ({
      id,
      version,
      idempotencyKey,
    }: {
      id: string;
      version: number;
      idempotencyKey: string;
    }) => api.withdrawRequest(id, version, idempotencyKey),
    onSuccess: (updated) => invalidate(updated.id),
  });
}

export function useDiscardRequest() {
  const invalidate = useInvalidateRequests();
  return useMutation({
    mutationFn: ({ id, version }: { id: string; version: number }) =>
      api.discardRequest(id, version),
    onSuccess: (updated) => invalidate(updated.id),
  });
}

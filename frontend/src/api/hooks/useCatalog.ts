/** 主数据相关的查询 hooks。
 *
 * 用 TanStack Query 包一层，把"请求状态 + 缓存 + 失效"都交给它：
 * 写操作成功后 invalidateQueries 会自动重拉，**永远不用手写"刷新列表"**。
 *
 * 公开列表与管理员列表用不同的 key —— 管理员列表含已停用项，
 * 写操作要同时让两边都失效。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, type ApprovalPolicy } from "../client";

const SUPPLIERS = ["suppliers"] as const;
const ADMIN_SUPPLIERS = ["admin", "suppliers"] as const;
const DEPARTMENTS = ["departments"] as const;
const CATEGORIES = ["categories"] as const;
const ADMIN_CATEGORIES = ["admin", "categories"] as const;
const BUDGETS = ["admin", "budgets"] as const;

// ------------------------------------------------------------------ 查询

export function useSuppliers() {
  return useQuery({ queryKey: SUPPLIERS, queryFn: api.listSuppliers });
}

export function useAdminSuppliers() {
  return useQuery({ queryKey: ADMIN_SUPPLIERS, queryFn: api.adminListSuppliers });
}

export function useDepartments() {
  return useQuery({ queryKey: DEPARTMENTS, queryFn: api.listDepartments });
}

export function useAdminCategories() {
  return useQuery({ queryKey: ADMIN_CATEGORIES, queryFn: api.adminListCategories });
}

export function useBudgets() {
  return useQuery({ queryKey: BUDGETS, queryFn: api.adminListBudgets });
}

// ------------------------------------------------------------------ 写操作

export function useCreateSupplier() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: api.adminCreateSupplier,
    onSuccess: () => {
      // 管理员列表和公开列表都要失效 —— 新供应商两边都看得到
      client.invalidateQueries({ queryKey: ADMIN_SUPPLIERS });
      client.invalidateQueries({ queryKey: SUPPLIERS });
    },
  });
}

export function useDisableSupplier() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, reason }: { id: string; reason: string }) =>
      api.adminDisableSupplier(id, reason),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ADMIN_SUPPLIERS });
      client.invalidateQueries({ queryKey: SUPPLIERS });
    },
  });
}

export function useCreateDepartment() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: api.adminCreateDepartment,
    onSuccess: () => client.invalidateQueries({ queryKey: DEPARTMENTS }),
  });
}

function invalidateCategories(client: ReturnType<typeof useQueryClient>) {
  client.invalidateQueries({ queryKey: ADMIN_CATEGORIES });
  client.invalidateQueries({ queryKey: CATEGORIES });
}

export function useCreateCategory() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: api.adminCreateCategory,
    onSuccess: () => invalidateCategories(client),
  });
}

export function useUpdateApprovalPolicy() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, policy }: { id: string; policy: ApprovalPolicy }) =>
      api.adminUpdateApprovalPolicy(id, policy),
    onSuccess: () => invalidateCategories(client),
  });
}

export function useCreateBudget() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: api.adminCreateBudget,
    onSuccess: () => client.invalidateQueries({ queryKey: BUDGETS }),
  });
}

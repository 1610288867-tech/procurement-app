/** 登录态相关的 hooks（M3）。
 *
 * 令牌全在 httpOnly Cookie 里，**前端拿不到也存不了** ——
 * 所以"我登录了吗"这个问题唯一的答案就是问后端 `GET /api/v1/auth/me`。
 * TanStack Query 顺手把答案缓存住，页面之间切换不用每次都问。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, type Account } from "../client";

const ME = ["auth", "me"] as const;

export function useMe() {
  return useQuery({
    queryKey: ME,
    queryFn: api.me,
    // 401 是"没登录"，不是"网络抖动"，重试没有意义
    retry: false,
    // 一分钟内不重复问；刚登录时我们会直接写缓存，不会读到旧值
    staleTime: 60_000,
  });
}

export function useLogin() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: api.login,
    // 后端已经返回了账号信息，直接写进缓存，省一次往返
    onSuccess: (account: Account) => client.setQueryData(ME, account),
  });
}

export function useLogout() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: api.logout,
    // 删掉缓存后守卫会重新问一次 /me，拿到 401 就跳登录页
    onSuccess: () => client.removeQueries({ queryKey: ME }),
  });
}

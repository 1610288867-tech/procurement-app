/** 登录页（M3）。
 *
 * 登录成功后后端把 access/refresh 两个令牌写进 **httpOnly Cookie**，
 * 前端拿到的只有账号信息 —— 所以这里没有 localStorage、没有令牌变量。
 */

import { useState, type FormEvent } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { ApiError } from "../api/client";
import { useLogin } from "../api/hooks/useAuth";

// 仅本地演示数据的账号（由 backend/scripts/seed.py 生成）
const DEMO_ACCOUNTS = [
  ["admin", "ADMIN + GM（主数据 + 总经理）"],
  ["requester", "REQUESTER（申请人）"],
  ["dept-manager", "DEPT_MANAGER（部门主管）"],
  ["dept-head", "DEPT_HEAD（部门负责人）"],
  ["procurement", "PROCUREMENT（采购专员）"],
  ["finance", "FINANCE（财务）"],
  ["receiver", "RECEIVER（收货人）"],
] as const;
const DEMO_PASSWORD = "Dev-Passw0rd!";

export default function Login() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const login = useLogin();
  const navigate = useNavigate();
  const location = useLocation();
  const from = (location.state as { from?: string } | null)?.from ?? "/";

  function submit(event: FormEvent) {
    event.preventDefault();
    login.mutate(
      { username, password },
      { onSuccess: () => navigate(from, { replace: true }) },
    );
  }

  const error = login.error;

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
      <h2 className="text-lg font-semibold text-slate-800">登录</h2>

      <form className="mt-4 flex flex-col gap-3" onSubmit={submit}>
        <label className="text-sm text-slate-600">
          用户名
          <input
            className="mt-1 block w-full rounded border border-slate-300 px-2 py-1 text-slate-900"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            autoComplete="username"
            required
          />
        </label>
        <label className="text-sm text-slate-600">
          密码
          <input
            className="mt-1 block w-full rounded border border-slate-300 px-2 py-1 text-slate-900"
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete="current-password"
            required
          />
        </label>
        <button
          type="submit"
          className="w-fit rounded bg-slate-900 px-4 py-1.5 text-sm text-white disabled:opacity-50"
          disabled={login.isPending}
        >
          {login.isPending ? "登录中…" : "登录"}
        </button>
      </form>

      {error instanceof ApiError && (
        <p className="mt-3 text-sm text-red-600">
          {error.message}
          {error.requestId && (
            <span className="ml-1 text-slate-400">（request_id: {error.requestId}）</span>
          )}
        </p>
      )}

      <details className="mt-5 text-sm text-slate-500">
        <summary className="cursor-pointer">演示账号（由 seed 脚本生成）</summary>
        <table className="mt-2 w-full text-left">
          <tbody>
            {DEMO_ACCOUNTS.map(([name, description]) => (
              <tr key={name} className="border-b border-slate-100">
                <td className="py-1 pr-4 font-mono text-slate-700">{name}</td>
                <td className="py-1">{description}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="mt-2">
          口令统一为 <span className="font-mono">{DEMO_PASSWORD}</span>（仅本地演示数据）。
        </p>
      </details>
    </section>
  );
}

/** 登录页（M3 · M4 重刷样式）。
 *
 * 登录成功后后端把 access/refresh 两个令牌写进 **httpOnly Cookie**，
 * 前端拿到的只有账号信息 —— 所以这里没有 localStorage、没有令牌变量。
 */

import { useState, type FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";

import { useLogin, useMe } from "../api/hooks/useAuth";
import { Button, Card, ErrorNote, Field, Input } from "../components/ui";

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
  // Edge/Chrome 自带的"显示密码"眼睛只在输入框有焦点时出现，一失焦就没了，
  // 想核对刚填的密码就得重新点回输入框。这里给一个**常驻**的开关补上这一点。
  const [showPassword, setShowPassword] = useState(false);
  const login = useLogin();
  const me = useMe();
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

  // 已经登录的人不该看到登录页：否则侧边栏显示"系统管理员 / 退出"，
  // 中间却摆着一张登录表单，同一屏里两个互相矛盾的入口。
  // 放在所有 hook 之后返回，避免条件调用 hook。
  if (me.data) {
    return <Navigate to={from} replace />;
  }

  const error = login.error;

  return (
    <div className="mx-auto max-w-md pt-8">
      <Card>
        <h1 className="text-xl font-semibold text-ink">登录</h1>
        <p className="mt-1 text-sm text-muted">
          企业采购审批系统 · 本地演示环境
        </p>

        <form className="mt-6 flex flex-col gap-4" onSubmit={submit}>
          <Field label="用户名">
            <Input
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              autoComplete="username"
              required
            />
          </Field>

          <Field label="密码">
            <span className="flex items-center gap-2">
              <Input
                className="w-full"
                type={showPassword ? "text" : "password"}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                autoComplete="current-password"
                required
              />
              <button
                type="button"
                onClick={() => setShowPassword((visible) => !visible)}
                aria-label={showPassword ? "隐藏密码" : "显示密码"}
                aria-pressed={showPassword}
                className="shrink-0 rounded-sm border border-line-strong px-2 py-1.5 text-xs text-muted hover:bg-hover"
              >
                {showPassword ? "隐藏" : "显示"}
              </button>
            </span>
          </Field>

          <Button type="submit" disabled={login.isPending} className="w-fit">
            {login.isPending ? "登录中…" : "登录"}
          </Button>
        </form>

        <ErrorNote error={error} />

        <details className="mt-6 text-sm text-muted">
          <summary className="cursor-pointer">演示账号（由 seed 脚本生成）</summary>
          <table className="mt-3 w-full text-left">
            <tbody>
              {DEMO_ACCOUNTS.map(([name, description]) => (
                <tr key={name} className="border-b border-line/60 last:border-0">
                  <td className="py-1.5 pr-4 font-mono text-body">{name}</td>
                  <td className="py-1.5">{description}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-2 text-xs text-faint">
            口令统一为 <span className="font-mono">{DEMO_PASSWORD}</span>
            （仅本地演示数据）。
          </p>
        </details>
      </Card>
    </div>
  );
}

/** 登录页（M3 · 分栏布局参考小米账号 / 知乎 / 高校融合门户的登录页）。
 *
 * ============================ 排版 ============================
 *   宽屏：左「品牌 + 这套系统能做什么」　右「表单」
 *   窄屏：只留表单（左侧介绍整块隐藏，不堆成上下两屏）
 *
 * 为什么值得单开一页、不套外壳：登录是**没有身份**的时刻 ——
 * 左边告诉他"这是个什么系统"，右边让他进来。侧边栏在这个时刻没有信息量。
 *
 * ============================ 三条不能碰的约定 ============================
 * 1. 登录成功后后端把 access/refresh 两个令牌写进 **httpOnly Cookie**，
 *    前端拿到的只有账号信息 —— 所以这里没有 localStorage、没有令牌变量。
 * 2. **本页第一个 `<form>` 必须是登录表单**，且用户名框带 `autocomplete="username"`、
 *    密码框 `type="password"`：浏览器自检就是按这三个选择器驱动登录的
 *    （见 `scripts/浏览器自检.mjs` 的 `loginExpr`）。左侧介绍里因此**不能放表单**。
 * 3. 「显示 / 隐藏」按钮要**常驻**：Edge/Chrome 自带的眼睛只在输入框有焦点时出现，
 *    一失焦就没了，想核对刚填的密码得重新点回输入框。
 */

import { useState, type FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";

import { useLogin, useMe } from "../api/hooks/useAuth";
import { Button, ErrorNote, Field, Input } from "../components/ui";

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

/** 左侧介绍：三条"这个系统真的做到了"的能力，不写形容词。 */
const HIGHLIGHTS = [
  ["动态多级审批", "金额决定层级，规则存在品类上 —— 改规则不用改代码"],
  ["预算并发预留", "条件更新扣减，两个人同时提交也只有一笔能占住额度"],
  ["差额显式释放", "实付小于预留时把差额放回去，账目永远对得上"],
  ["全过程可追溯", "事件只追加、策略留快照，三个月后还能解释当时的审批链"],
] as const;

export default function Login() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
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

  // 已经登录的人不该看到登录页：否则会出现"已登录却还摆着登录表单"的矛盾画面。
  // 放在所有 hook 之后返回，避免条件调用 hook。
  if (me.data) {
    return <Navigate to={from} replace />;
  }

  return (
    <div className="grid min-h-screen lg:grid-cols-[1.05fr_minmax(380px,0.95fr)]">
      {/* ---------------- 左：品牌与能力介绍（窄屏隐藏） ---------------- */}
      <aside className="relative hidden flex-col justify-between overflow-hidden border-r border-line bg-section p-10 lg:flex xl:p-14">
        {/* 一层很淡的径向渐变，只为了不让大块底色太平；颜色全部取自令牌 */}
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-0 opacity-70"
          style={{
            background:
              "radial-gradient(120% 80% at 15% 0%, var(--color-card) 0%, transparent 55%)," +
              "radial-gradient(90% 70% at 100% 100%, var(--color-hover) 0%, transparent 60%)",
          }}
        />

        <div className="relative">
          <div className="flex items-center gap-3">
            <span className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary text-base font-semibold text-primary-fg">
              采
            </span>
            <span>
              <span className="block text-base font-semibold text-ink">
                企业采购审批系统
              </span>
              <span className="block text-xs text-muted">
                个人学习项目 · Windows 浏览器优先
              </span>
            </span>
          </div>

          <h1 className="mt-12 max-w-md text-3xl font-semibold leading-snug text-ink">
            从一张请购单
            <br />
            到付款归档的全过程
          </h1>
          <p className="mt-3 max-w-md text-sm text-muted">
            申请 → 动态多级审批 → 询价定标 → 下单收货 → 三单匹配 → 付款 → 归档。
            每一个环节都留下痕迹，每一笔钱都对得上。
          </p>

          <ul className="mt-10 space-y-5">
            {HIGHLIGHTS.map(([title, detail]) => (
              <li key={title} className="flex gap-3">
                <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full border border-line-strong bg-card">
                  <svg
                    aria-hidden="true"
                    viewBox="0 0 24 24"
                    className="h-3 w-3 text-success"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2.4"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  >
                    <path d="m5 13 4 4L19 7" />
                  </svg>
                </span>
                <span>
                  <span className="block text-sm font-medium text-ink">{title}</span>
                  <span className="mt-0.5 block text-xs leading-relaxed text-muted">
                    {detail}
                  </span>
                </span>
              </li>
            ))}
          </ul>
        </div>

        <p className="relative mt-10 text-xs text-faint">
          本地演示环境 · 全部数据为合成数据，不接真实供应商与资金
        </p>
      </aside>

      {/* ---------------- 右：表单 ---------------- */}
      <div className="flex items-center justify-center px-6 py-10 sm:px-10">
        <div className="w-full max-w-sm">
          {/* 窄屏时左侧整块隐藏，这里补一个品牌头，避免进来看不到"这是什么系统" */}
          <div className="mb-8 flex items-center gap-3 lg:hidden">
            <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary text-sm font-semibold text-primary-fg">
              采
            </span>
            <span className="text-sm font-semibold text-ink">企业采购审批系统</span>
          </div>

          <h2 className="text-2xl font-semibold text-ink">登录</h2>
          <p className="mt-1 text-sm text-muted">
            用演示账号进入，或找管理员要一个账号。
          </p>

          <form className="mt-8 flex flex-col gap-4" onSubmit={submit}>
            <Field label="用户名">
              <Input
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                autoComplete="username"
                placeholder="例如 admin"
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
                  placeholder="Dev-Passw0rd!"
                  required
                />
                <button
                  type="button"
                  onClick={() => setShowPassword((visible) => !visible)}
                  aria-label={showPassword ? "隐藏密码" : "显示密码"}
                  aria-pressed={showPassword}
                  className="shrink-0 rounded-md border border-line-strong px-2.5 py-2 text-xs text-muted transition-colors hover:bg-hover hover:text-ink"
                >
                  {showPassword ? "隐藏" : "显示"}
                </button>
              </span>
            </Field>

            <Button type="submit" block disabled={login.isPending}>
              {login.isPending ? "登录中…" : "登录"}
            </Button>
          </form>

          <ErrorNote error={login.error} />

          <details className="group mt-8 rounded-lg border border-line bg-card px-4 py-3 text-sm">
            <summary className="cursor-pointer list-none text-muted transition-colors hover:text-ink">
              <span className="text-xs">演示账号（由 seed 脚本生成）</span>
              <span className="float-right text-xs text-faint group-open:hidden">展开</span>
              <span className="float-right hidden text-xs text-faint group-open:inline">
                收起
              </span>
            </summary>
            <table className="mt-3 w-full text-left text-xs">
              <tbody>
                {DEMO_ACCOUNTS.map(([name, description]) => (
                  <tr key={name} className="border-b border-line/60 last:border-0">
                    <td className="py-1.5 pr-3 font-mono text-body">{name}</td>
                    <td className="py-1.5 text-muted">{description}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="mt-2 text-xs text-faint">
              口令统一为 <span className="font-mono">{DEMO_PASSWORD}</span>（仅本地演示数据）。
            </p>
          </details>
        </div>
      </div>
    </div>
  );
}

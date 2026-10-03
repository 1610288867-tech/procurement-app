/** 路由守卫（M3 建 · M4 重刷样式 · 2026-10-03 加"需要部门"）。
 *
 * 两条分工要说清楚：
 *   - **前端守卫是体验**：别让人看到点不动的菜单、别白屏。
 *   - **后端校验才是安全**：用 Postman 直接打接口就绕过了这里。
 * 所以下面这些"无权限"提示从来不当作安全措施。
 *
 * ============================ 为什么守卫要知道"部门" ============================
 * "能不能发起采购申请"不看角色，看**有没有归属部门**：部门决定这笔钱从哪条预算出、
 * 审批链找谁（`DEPT_*` 按部门含子部门解析）、以及谁能看见这张单子。
 * 所以系统管理员（`ADMIN`，按设计**无部门**）能管主数据但发起不了申请 ——
 * 判据必须与后端 `NO_DEPARTMENT` 那条**同源**，而不是"角色是不是 ADMIN"
 * （哪天给某个管理员指定了部门，他照样应该能申请）。
 *
 * 这里只做**入口**把关；真正的边界仍是后端那条 422。
 */

import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";

import { useMe } from "../api/hooks/useAuth";
import { Card, HintNote, Loading } from "./ui";

export default function RequireAuth({
  roles,
  requireDepartment = false,
  children,
}: {
  /** 留空表示"登录就行" */
  roles?: string[];
  /** 为真时要求账号有归属部门（发起采购申请的前提） */
  requireDepartment?: boolean;
  children: ReactNode;
}) {
  const me = useMe();
  const location = useLocation();

  if (me.isPending) {
    return (
      <Card>
        <Loading label="检查登录状态…" />
      </Card>
    );
  }

  if (me.isError) {
    // 记住原来想去哪，登录后直接送过去
    return <Navigate to="/login" state={{ from: location.pathname }} replace />;
  }

  if (roles && roles.length > 0) {
    const allowed = me.data.roles.some((role) => roles.includes(role));
    if (!allowed) {
      return <Forbidden required={roles} held={me.data.roles} />;
    }
  }

  if (requireDepartment && !me.data.department_id) {
    return <NoDepartment />;
  }

  return <>{children}</>;
}

function Forbidden({ required, held }: { required: string[]; held: string[] }) {
  return (
    <Card>
      <h1 className="text-lg font-semibold text-ink">无权限</h1>
      <p className="mt-2 text-sm text-body">
        这个页面需要 <span className="font-mono">{required.join(" / ")}</span>，
        当前账号的角色是{" "}
        <span className="font-mono">{held.join(" / ") || "（无）"}</span>。
      </p>
      <div className="mt-3">
        <HintNote>
          换一个账号再试。菜单按角色渲染只是体验优化，后端每个接口都会独立校验。
        </HintNote>
      </div>
    </Card>
  );
}

/** 账号没有归属部门时的说明页。
 *
 * 不说"无权限"—— 这是**账号配置**问题，不是权限问题：给他指定一个部门就能用了。 */
function NoDepartment() {
  return (
    <Card>
      <h1 className="text-lg font-semibold text-ink">这个账号没有所属部门</h1>
      <p className="mt-2 text-sm text-body">
        采购申请必须有归属部门 —— 它决定这笔钱从哪条预算出、审批链找谁、
        以及谁能看到这张单子。所以没有部门的账号无法发起申请。
      </p>
      <div className="mt-3">
        <HintNote>
          请联系管理员给这个账号指定所属部门。若这个账号本来就不该发起采购
          （例如系统管理员），请改用需要采购的部门账号提交。
        </HintNote>
      </div>
    </Card>
  );
}

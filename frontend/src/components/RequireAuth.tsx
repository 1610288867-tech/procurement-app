/** 路由守卫（M3 · M4 重刷样式）。
 *
 * 两条分工要说清楚：
 *   - **前端守卫是体验**：别让人看到点不动的菜单、别白屏。
 *   - **后端校验才是安全**：用 Postman 直接打接口就绕过了这里。
 * 所以下面这些"无权限"提示从来不当作安全措施。
 */

import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";

import { useMe } from "../api/hooks/useAuth";
import { Card, HintNote, Loading } from "./ui";

export default function RequireAuth({
  roles,
  children,
}: {
  /** 留空表示"登录就行" */
  roles?: string[];
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

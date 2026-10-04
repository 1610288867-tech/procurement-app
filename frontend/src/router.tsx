/** 路由表。
 *
 * M3 起分三层：
 *   /            公开（环境自检，排查用）
 *   /login       公开
 *   其余业务页   要求登录；/admin 还要求 ADMIN 角色
 *
 * 守卫在 `components/RequireAuth.tsx`。它只管体验，
 * **真正的权限由后端独立校验** —— 前端藏菜单拦不住 Postman。
 */

import { Route, Routes } from "react-router-dom";

import RequireAuth from "./components/RequireAuth";
import Home from "./pages/Home";
import Login from "./pages/Login";
import SupplierList from "./pages/SupplierList";
import TaskDetail from "./pages/approver/TaskDetail";
import TaskList from "./pages/approver/TaskList";
import RequestDetail from "./pages/requester/RequestDetail";
import RequestList from "./pages/requester/RequestList";
import RequestNew from "./pages/requester/RequestNew";
import AdminBudgets from "./pages/admin/Budgets";
import AdminCategories from "./pages/admin/Categories";
import AdminSuppliers from "./pages/admin/Suppliers";
import BudgetBoard from "./pages/finance/BudgetBoard";

export default function AppRoutes() {
  return (
    <Routes>
      <Route path="/" element={<Home />} />
      <Route path="/login" element={<Login />} />

      <Route
        path="/suppliers"
        element={
          <RequireAuth>
            <SupplierList />
          </RequireAuth>
        }
      />

      {/* 采购申请（M5）：不限角色，但**要求账号有归属部门**。
          没有部门的账号（例如系统管理员）发起不了申请 —— 部门决定预算与审批链。
          这一层只是"别让人白填一整张表"，真正的边界仍是后端那条 422 `NO_DEPARTMENT`。 */}
      <Route
        path="/requests"
        element={
          <RequireAuth requireDepartment>
            <RequestList />
          </RequireAuth>
        }
      />
      <Route
        path="/requests/new"
        element={
          <RequireAuth requireDepartment>
            <RequestNew />
          </RequireAuth>
        }
      />
      <Route
        path="/requests/:id"
        element={
          <RequireAuth requireDepartment>
            <RequestDetail />
          </RequireAuth>
        }
      />

      {/* 审批（M6）：不限角色，登录即可 ——
          「我的待办」对谁都是空的还是满的，由**后端按当前级过滤**决定，
          前端不预先按角色判断（那样只会把"该不该看到"讲成两套规则）。 */}
      <Route
        path="/approvals/tasks"
        element={
          <RequireAuth>
            <TaskList />
          </RequireAuth>
        }
      />
      <Route
        path="/approvals/tasks/:id"
        element={
          <RequireAuth>
            <TaskDetail />
          </RequireAuth>
        }
      />

      {/* 预算看板（M7）：**只读**，财务与管理员可见。
          "看钱"与"改钱"刻意分成两个入口 —— 改预算仍然只走 /admin/budgets。 */}
      <Route
        path="/budgets"
        element={
          <RequireAuth roles={["FINANCE", "ADMIN"]}>
            <BudgetBoard />
          </RequireAuth>
        }
      />

      <Route
        path="/admin/suppliers"
        element={
          <RequireAuth roles={["ADMIN"]}>
            <AdminSuppliers />
          </RequireAuth>
        }
      />
      <Route
        path="/admin/categories"
        element={
          <RequireAuth roles={["ADMIN"]}>
            <AdminCategories />
          </RequireAuth>
        }
      />
      <Route
        path="/admin/budgets"
        element={
          <RequireAuth roles={["ADMIN"]}>
            <AdminBudgets />
          </RequireAuth>
        }
      />
    </Routes>
  );
}

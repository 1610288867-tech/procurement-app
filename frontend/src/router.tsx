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
import AdminBudgets from "./pages/admin/Budgets";
import AdminCategories from "./pages/admin/Categories";
import AdminSuppliers from "./pages/admin/Suppliers";

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

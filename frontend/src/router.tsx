/** 路由表。
 *
 * M2 起分成两块：公开只读页（/）与管理端（/admin）。
 * M3 会给 /admin 加上按角色的守卫 —— 现在还没有登录态。
 */

import { Route, Routes } from "react-router-dom";

import Home from "./pages/Home";
import SupplierList from "./pages/SupplierList";
import AdminBudgets from "./pages/admin/Budgets";
import AdminCategories from "./pages/admin/Categories";
import AdminSuppliers from "./pages/admin/Suppliers";

export default function AppRoutes() {
  return (
    <Routes>
      <Route path="/" element={<Home />} />
      <Route path="/suppliers" element={<SupplierList />} />

      <Route path="/admin/suppliers" element={<AdminSuppliers />} />
      <Route path="/admin/categories" element={<AdminCategories />} />
      <Route path="/admin/budgets" element={<AdminBudgets />} />
    </Routes>
  );
}

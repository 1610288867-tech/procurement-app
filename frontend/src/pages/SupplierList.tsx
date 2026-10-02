/** 公开的供应商列表（只读）。
 *
 * M2 起，新增与停用都移到管理端 `/admin/suppliers`；
 * 这里的 `/api/v1/suppliers` 只返回启用中的供应商，任何角色都能看。
 */

import { Link } from "react-router-dom";

import { useSuppliers } from "../api/hooks/useCatalog";

export default function SupplierList() {
  const suppliers = useSuppliers();

  return (
    <section className="rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
      <div className="mb-3 flex items-baseline justify-between">
        <h2 className="text-lg font-semibold text-slate-800">合格供应商</h2>
        <Link to="/admin/suppliers" className="text-sm text-blue-600 hover:underline">
          管理供应商 →
        </Link>
      </div>

      {suppliers.isPending && <p className="text-slate-500">加载中…</p>}
      {suppliers.isError && (
        <p className="text-red-600">{(suppliers.error as Error).message}</p>
      )}
      {suppliers.data && suppliers.data.length === 0 && (
        <p className="text-slate-500">
          还没有供应商。去{" "}
          <Link to="/admin/suppliers" className="text-blue-600 hover:underline">
            供应商管理
          </Link>{" "}
          加一个。
        </p>
      )}

      {suppliers.data && suppliers.data.length > 0 && (
        <table className="w-full text-left text-sm">
          <thead className="border-b border-slate-200 text-slate-500">
            <tr>
              <th className="py-2 font-medium">编码</th>
              <th className="py-2 font-medium">名称</th>
              <th className="py-2 font-medium">资质到期</th>
            </tr>
          </thead>
          <tbody>
            {suppliers.data.map((supplier) => (
              <tr key={supplier.id} className="border-b border-slate-100">
                <td className="py-2 font-mono text-slate-700">{supplier.code}</td>
                <td className="py-2 text-slate-800">{supplier.name}</td>
                <td className="py-2 text-slate-600">
                  {supplier.qualification_expires_at?.slice(0, 10) ?? "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

/** 合格供应商列表（只读）。
 *
 * M2 起，新增与停用都移到管理端 `/admin/suppliers`；
 * 这里的 `/api/v1/suppliers` 只返回**合格**供应商（未停用且资质未过期），
 * **任何登录用户**都能看（2026-10-03 起要求登录，不再是公开接口）。
 *
 * ============================ 指向管理端的入口要按角色显示 ============================
 * "管理供应商"是 ADMIN 的事。对别的角色显示它，只会让人点进去撞一个 403 ——
 * 这正是本项目一开始想避免的"看得到却做不了的入口"。
 * （界面判断只是体验优化：`/admin/*` 在后端仍然由 `require_roles(ADMIN)` 兜底。）
 */

import { Link } from "react-router-dom";

import { useMe } from "../api/hooks/useAuth";
import { useSuppliers } from "../api/hooks/useCatalog";
import {
  Card,
  Cell,
  EmptyState,
  ErrorNote,
  Loading,
  PageHeader,
  Row,
  Table,
} from "../components/ui";

export default function SupplierList() {
  const suppliers = useSuppliers();
  const me = useMe();
  const isAdmin = me.data?.roles.includes("ADMIN") ?? false;

  return (
    <>
      <PageHeader
        title="合格供应商"
        description="只列出未停用、且资质未过期的供应商。"
        action={
          // 只有管理员才该看到这个入口
          isAdmin ? (
            <Link to="/admin/suppliers" className="text-sm text-body hover:underline">
              管理供应商 →
            </Link>
          ) : undefined
        }
      />

      <Card>
        {suppliers.isPending && <Loading />}
        <ErrorNote error={suppliers.error} />

        {suppliers.data && suppliers.data.length === 0 && (
          <EmptyState>
            {isAdmin ? (
              <>
                还没有供应商。去{" "}
                <Link to="/admin/suppliers" className="text-body underline">
                  供应商管理
                </Link>{" "}
                加一个。
              </>
            ) : (
              // 非管理员看到的是"结果为空"，而不是一个他点进去会 403 的入口
              <>当前没有可选用的供应商，请联系采购部门维护合格供应商清单。</>
            )}
          </EmptyState>
        )}

        {suppliers.data && suppliers.data.length > 0 && (
          <Table head={["编码", "名称", "资质到期"]}>
            {suppliers.data.map((supplier) => (
              <Row key={supplier.id}>
                <Cell className="font-mono text-muted">{supplier.code}</Cell>
                <Cell>{supplier.name}</Cell>
                <Cell className="text-muted">
                  {supplier.qualification_expires_at?.slice(0, 10) ?? "—"}
                </Cell>
              </Row>
            ))}
          </Table>
        )}
      </Card>
    </>
  );
}

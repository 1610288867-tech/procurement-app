/** 公开的供应商列表（只读）。
 *
 * M2 起，新增与停用都移到管理端 `/admin/suppliers`；
 * 这里的 `/api/v1/suppliers` 只返回**合格**供应商（未停用且资质未过期），
 * 任何角色都能看。
 */

import { Link } from "react-router-dom";

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

  return (
    <>
      <PageHeader
        title="合格供应商"
        description="只列出未停用、且资质未过期的供应商。"
        action={
          <Link to="/admin/suppliers" className="text-sm text-body hover:underline">
            管理供应商 →
          </Link>
        }
      />

      <Card>
        {suppliers.isPending && <Loading />}
        <ErrorNote error={suppliers.error} />

        {suppliers.data && suppliers.data.length === 0 && (
          <EmptyState>
            还没有供应商。去{" "}
            <Link to="/admin/suppliers" className="text-body underline">
              供应商管理
            </Link>{" "}
            加一个。
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

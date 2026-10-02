/** 供应商管理：新增 + 停用（停用要填原因，业务规则要求留痕）。
 *
 * 公开的 /suppliers 只读；写操作都在 /admin 下，M3 起要求 ADMIN 角色。
 * 这一页刻意列出**含已停用**的全部供应商 —— 公开列表按规则藏起来的，
 * 管理端必须留有痕迹，否则"这家怎么不见了"没法排查。
 */

import { useState, type FormEvent } from "react";

import {
  useAdminSuppliers,
  useCreateSupplier,
  useDisableSupplier,
} from "../../api/hooks/useCatalog";
import {
  Button,
  Card,
  Cell,
  EmptyState,
  ErrorNote,
  Field,
  Input,
  Loading,
  PageHeader,
  Row,
  StatusBadge,
  Table,
} from "../../components/ui";

export default function AdminSuppliers() {
  const suppliers = useAdminSuppliers();
  const createSupplier = useCreateSupplier();
  const disableSupplier = useDisableSupplier();

  const [name, setName] = useState("");
  const [code, setCode] = useState("");
  const [expiry, setExpiry] = useState("");
  const [disableTarget, setDisableTarget] = useState<string | null>(null);
  const [reason, setReason] = useState("");

  function handleCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    createSupplier.mutate(
      {
        name: name.trim(),
        code: code.trim(),
        qualification_expires_at: expiry ? `${expiry}T00:00:00+08:00` : null,
      },
      {
        onSuccess: () => {
          setName("");
          setCode("");
          setExpiry("");
        },
      },
    );
  }

  function handleDisable(id: string) {
    disableSupplier.mutate(
      { id, reason: reason.trim() },
      {
        onSuccess: () => {
          setDisableTarget(null);
          setReason("");
        },
      },
    );
  }

  return (
    <>
      <PageHeader
        title="供应商管理"
        description="停用必须填原因（业务规则要求留痕）；已停用与资质过期的都会列在这里。"
      />

      <Card title="新增供应商">
        <form onSubmit={handleCreate} className="flex flex-wrap items-end gap-4">
          <Field label="名称">
            <Input
              value={name}
              onChange={(event) => setName(event.target.value)}
              required
              maxLength={200}
            />
          </Field>
          <Field label="编码">
            <Input
              className="font-mono"
              value={code}
              onChange={(event) => setCode(event.target.value)}
              required
              maxLength={64}
              placeholder="SUP-001"
            />
          </Field>
          <Field label="资质有效期" hint="留空 = 长期有效">
            <Input
              type="date"
              value={expiry}
              onChange={(event) => setExpiry(event.target.value)}
            />
          </Field>
          <Button type="submit" disabled={createSupplier.isPending}>
            {createSupplier.isPending ? "提交中…" : "新增"}
          </Button>
        </form>
        <ErrorNote error={createSupplier.error} />
      </Card>

      <Card title="供应商（含已停用）">
        {suppliers.isPending && <Loading />}
        <ErrorNote error={suppliers.error} />

        {suppliers.data && suppliers.data.length === 0 && (
          <EmptyState>还没有供应商。</EmptyState>
        )}

        {suppliers.data && suppliers.data.length > 0 && (
          <Table head={["编码", "名称", "资质到期", "状态", "操作"]}>
            {suppliers.data.map((supplier) => (
              <Row key={supplier.id} className="align-top">
                <Cell className="font-mono text-muted">{supplier.code}</Cell>
                <Cell>{supplier.name}</Cell>
                <Cell className="text-muted">
                  {supplier.qualification_expires_at?.slice(0, 10) ?? "—"}
                </Cell>
                <Cell>
                  <StatusBadge active={supplier.active} />
                </Cell>
                <Cell>
                  {!supplier.active && <span className="text-faint">—</span>}
                  {supplier.active && disableTarget !== supplier.id && (
                    <button
                      onClick={() => {
                        setDisableTarget(supplier.id);
                        setReason("");
                      }}
                      className="text-sm text-danger hover:underline"
                    >
                      停用
                    </button>
                  )}
                  {supplier.active && disableTarget === supplier.id && (
                    <div className="flex items-center gap-2">
                      <Input
                        autoFocus
                        value={reason}
                        onChange={(event) => setReason(event.target.value)}
                        placeholder="停用原因"
                        maxLength={200}
                        className="w-40"
                      />
                      <Button
                        variant="danger"
                        className="px-2 py-1 text-xs"
                        disabled={reason.trim() === "" || disableSupplier.isPending}
                        onClick={() => handleDisable(supplier.id)}
                      >
                        确认
                      </Button>
                      <button
                        onClick={() => setDisableTarget(null)}
                        className="text-xs text-muted hover:underline"
                      >
                        取消
                      </button>
                    </div>
                  )}
                </Cell>
              </Row>
            ))}
          </Table>
        )}
        <ErrorNote error={disableSupplier.error} />
      </Card>
    </>
  );
}

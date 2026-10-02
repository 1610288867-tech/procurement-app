/** 供应商管理：新增 + 停用（停用要填原因，业务规则要求留痕）。
 *
 * 公开的 /suppliers 只读；写操作都在 /admin 下，M3 会给它加上角色依赖。
 */

import { useState, type FormEvent } from "react";

import { useAdminSuppliers, useCreateSupplier, useDisableSupplier } from "../../api/hooks/useCatalog";
import { FormError, Section, inputClass } from "./_ui";

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
    <section className="space-y-6">
      <Section title="新增供应商">
        <form onSubmit={handleCreate} className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col text-sm text-slate-600">
            名称
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              required
              maxLength={200}
              className={inputClass}
            />
          </label>
          <label className="flex flex-col text-sm text-slate-600">
            编码
            <input
              value={code}
              onChange={(event) => setCode(event.target.value)}
              required
              maxLength={64}
              placeholder="SUP-001"
              className={`${inputClass} font-mono`}
            />
          </label>
          <label className="flex flex-col text-sm text-slate-600">
            资质有效期（可选）
            <input
              type="date"
              value={expiry}
              onChange={(event) => setExpiry(event.target.value)}
              className={inputClass}
            />
          </label>
          <button
            type="submit"
            disabled={createSupplier.isPending}
            className="rounded bg-slate-800 px-4 py-1.5 text-sm text-white disabled:opacity-50"
          >
            {createSupplier.isPending ? "提交中…" : "新增"}
          </button>
        </form>
        <FormError error={createSupplier.error} />
      </Section>

      <Section title="供应商（含已停用）">
        {suppliers.isPending && <p className="text-slate-500">加载中…</p>}
        {suppliers.isError && (
          <p className="text-red-600">{(suppliers.error as Error).message}</p>
        )}
        {suppliers.data && suppliers.data.length === 0 && (
          <p className="text-slate-500">还没有供应商。</p>
        )}
        {suppliers.data && suppliers.data.length > 0 && (
          <table className="w-full text-left text-sm">
            <thead className="border-b border-slate-200 text-slate-500">
              <tr>
                <th className="py-2 font-medium">编码</th>
                <th className="py-2 font-medium">名称</th>
                <th className="py-2 font-medium">资质到期</th>
                <th className="py-2 font-medium">状态</th>
                <th className="py-2 font-medium">操作</th>
              </tr>
            </thead>
            <tbody>
              {suppliers.data.map((supplier) => (
                <tr key={supplier.id} className="border-b border-slate-100 align-top">
                  <td className="py-2 font-mono text-slate-700">{supplier.code}</td>
                  <td className="py-2 text-slate-800">{supplier.name}</td>
                  <td className="py-2 text-slate-600">
                    {supplier.qualification_expires_at?.slice(0, 10) ?? "—"}
                  </td>
                  <td className="py-2">
                    <span
                      className={
                        supplier.active
                          ? "rounded bg-green-100 px-2 py-0.5 text-xs text-green-800"
                          : "rounded bg-slate-100 px-2 py-0.5 text-xs text-slate-600"
                      }
                    >
                      {supplier.active ? "启用" : "停用"}
                    </span>
                  </td>
                  <td className="py-2">
                    {!supplier.active && <span className="text-slate-400">—</span>}
                    {supplier.active && disableTarget !== supplier.id && (
                      <button
                        onClick={() => {
                          setDisableTarget(supplier.id);
                          setReason("");
                        }}
                        className="text-sm text-red-600 hover:underline"
                      >
                        停用
                      </button>
                    )}
                    {supplier.active && disableTarget === supplier.id && (
                      <div className="flex items-center gap-2">
                        <input
                          autoFocus
                          value={reason}
                          onChange={(event) => setReason(event.target.value)}
                          placeholder="停用原因"
                          maxLength={200}
                          className="w-40 rounded border border-slate-300 px-2 py-1 text-sm"
                        />
                        <button
                          disabled={reason.trim() === "" || disableSupplier.isPending}
                          onClick={() => handleDisable(supplier.id)}
                          className="rounded bg-red-600 px-2 py-1 text-xs text-white disabled:opacity-50"
                        >
                          确认
                        </button>
                        <button
                          onClick={() => setDisableTarget(null)}
                          className="text-xs text-slate-500 hover:underline"
                        >
                          取消
                        </button>
                      </div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <FormError error={disableSupplier.error} />
      </Section>
    </section>
  );
}

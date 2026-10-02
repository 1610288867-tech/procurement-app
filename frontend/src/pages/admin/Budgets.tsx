/** 预算：部门 × 年度 × 品类。
 *
 * 界面填「元」，请求体发「分」，库里存「分」——换算只走 `lib/money.ts`。
 * 三个金额字段（总额 / 已预留 / 已花）现在就都展示出来，
 * 虽然 M6 才会真正动它们。
 */

import { useState, type FormEvent } from "react";

import {
  useBudgets,
  useCreateBudget,
  useDepartments,
  useAdminCategories,
} from "../../api/hooks/useCatalog";
import { fenToYuan, formatFen, yuanToFen } from "../../lib/money";
import { FormError, Section, inputClass } from "./_ui";

export default function AdminBudgets() {
  const departments = useDepartments();
  const categories = useAdminCategories();
  const budgets = useBudgets();
  const createBudget = useCreateBudget();

  const [departmentId, setDepartmentId] = useState("");
  const [categoryId, setCategoryId] = useState("");
  const [year, setYear] = useState(String(new Date().getFullYear()));
  const [amountYuan, setAmountYuan] = useState("1000");
  const [localError, setLocalError] = useState<string | null>(null);

  const departmentName = new Map(
    (departments.data ?? []).map((item) => [item.id, item.name]),
  );
  const categoryName = new Map(
    (categories.data ?? []).map((item) => [item.id, item.name]),
  );

  const ready = (departments.data?.length ?? 0) > 0 && (categories.data?.length ?? 0) > 0;

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setLocalError(null);

    let amountFen: number;
    try {
      // 界面是「元」，这里换成「分」——全项目唯一允许做这个换算的地方
      amountFen = yuanToFen(amountYuan);
    } catch (error) {
      setLocalError((error as Error).message);
      return;
    }

    createBudget.mutate(
      {
        department_id: departmentId,
        category_id: categoryId,
        year: Number(year),
        amount_fen: amountFen,
      },
      { onSuccess: () => setAmountYuan("1000") },
    );
  }

  return (
    <section className="space-y-6">
      <Section title="新增预算">
        {!ready && (
          <p className="mb-3 text-sm text-amber-700">
            先要有至少一个部门和采购品类，才能配预算。
          </p>
        )}
        <form onSubmit={handleSubmit} className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col text-sm text-slate-600">
            部门
            <select
              value={departmentId}
              onChange={(event) => setDepartmentId(event.target.value)}
              required
              className={inputClass}
            >
              <option value="">请选择</option>
              {(departments.data ?? []).map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col text-sm text-slate-600">
            品类
            <select
              value={categoryId}
              onChange={(event) => setCategoryId(event.target.value)}
              required
              className={inputClass}
            >
              <option value="">请选择</option>
              {(categories.data ?? []).map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col text-sm text-slate-600">
            年度
            <input
              type="number"
              min={2000}
              max={2100}
              value={year}
              onChange={(event) => setYear(event.target.value)}
              required
              className={`${inputClass} w-24`}
            />
          </label>
          <label className="flex flex-col text-sm text-slate-600">
            预算金额（元）
            <input
              value={amountYuan}
              onChange={(event) => setAmountYuan(event.target.value)}
              required
              className={`${inputClass} w-32 text-right`}
            />
          </label>
          <button
            type="submit"
            disabled={!ready || createBudget.isPending}
            className="rounded bg-slate-800 px-4 py-1.5 text-sm text-white disabled:opacity-50"
          >
            {createBudget.isPending ? "提交中…" : "新增"}
          </button>
        </form>
        <p className="mt-2 text-xs text-slate-500">
          填 1000 元 → 请求体发 <code>100000</code>（分）→ 库里也是 <code>100000</code>。
        </p>
        {localError && <p className="mt-3 text-sm text-red-600">{localError}</p>}
        <FormError error={createBudget.error} />
      </Section>

      <Section title="预算列表">
        {budgets.isPending && <p className="text-slate-500">加载中…</p>}
        {budgets.isError && <p className="text-red-600">{(budgets.error as Error).message}</p>}
        {budgets.data && budgets.data.length === 0 && (
          <p className="text-slate-500">还没有预算。</p>
        )}
        {budgets.data && budgets.data.length > 0 && (
          <table className="w-full text-left text-sm">
            <thead className="border-b border-slate-200 text-slate-500">
              <tr>
                <th className="py-2 font-medium">部门</th>
                <th className="py-2 font-medium">品类</th>
                <th className="py-2 font-medium">年度</th>
                <th className="py-2 text-right font-medium">总额</th>
                <th className="py-2 text-right font-medium">已预留</th>
                <th className="py-2 text-right font-medium">已花</th>
                <th className="py-2 text-right font-medium">可用</th>
              </tr>
            </thead>
            <tbody>
              {budgets.data.map((budget) => {
                const available =
                  budget.amount_total_fen -
                  budget.amount_reserved_fen -
                  budget.amount_spent_fen;
                return (
                  <tr key={budget.id} className="border-b border-slate-100">
                    <td className="py-2 text-slate-800">
                      {departmentName.get(budget.department_id) ?? "—"}
                    </td>
                    <td className="py-2 text-slate-800">
                      {categoryName.get(budget.category_id) ?? "—"}
                    </td>
                    <td className="py-2 text-slate-600">{budget.year}</td>
                    <td className="py-2 text-right font-mono text-slate-700">
                      {formatFen(budget.amount_total_fen)}
                    </td>
                    <td className="py-2 text-right font-mono text-slate-700">
                      {formatFen(budget.amount_reserved_fen)}
                    </td>
                    <td className="py-2 text-right font-mono text-slate-700">
                      {formatFen(budget.amount_spent_fen)}
                    </td>
                    <td
                      className={
                        available > 0
                          ? "py-2 text-right font-mono text-green-700"
                          : "py-2 text-right font-mono text-red-600"
                      }
                      title={`${fenToYuan(available)} 元`}
                    >
                      {formatFen(available)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Section>
    </section>
  );
}

/** 预算：部门 × 年度 × 品类（M4 重刷样式）。
 *
 * 界面填「元」，请求体发「分」，库里存「分」——换算只走 `lib/money.ts`。
 * 三个金额字段（总额 / 已预留 / 已花）现在就都展示出来，
 * 虽然 M6 才会真正动它们。
 */

import { useState, type FormEvent } from "react";

import {
  useAdminCategories,
  useBudgets,
  useCreateBudget,
  useDepartments,
} from "../../api/hooks/useCatalog";
import { fenToYuan, formatFen, yuanToFen } from "../../lib/money";
import {
  Button,
  Card,
  Cell,
  EmptyState,
  ErrorNote,
  Field,
  HintNote,
  Input,
  Loading,
  PageHeader,
  Row,
  Select,
  Table,
} from "../../components/ui";

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
    <>
      <PageHeader
        title="预算"
        description="部门 × 年度 × 品类；金额界面显示「元」，库里存「分」。"
        breadcrumb={[{ label: "首页", to: "/" }, { label: "管理员" }, { label: "预算" }]}
      />

      <Card title="新增预算">
        {!ready && (
          <div className="mb-4">
            <HintNote>先要有至少一个部门和采购品类，才能配预算。</HintNote>
          </div>
        )}
        <form onSubmit={handleSubmit} className="flex flex-wrap items-end gap-4">
          <Field label="部门">
            <Select
              value={departmentId}
              onChange={(event) => setDepartmentId(event.target.value)}
              required
            >
              <option value="">请选择</option>
              {(departments.data ?? []).map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </Select>
          </Field>

          <Field label="品类">
            <Select
              value={categoryId}
              onChange={(event) => setCategoryId(event.target.value)}
              required
            >
              <option value="">请选择</option>
              {(categories.data ?? []).map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </Select>
          </Field>

          <Field label="年度">
            <Input
              className="w-24"
              type="number"
              min={2000}
              max={2100}
              value={year}
              onChange={(event) => setYear(event.target.value)}
              required
            />
          </Field>

          <Field label="预算金额（元）">
            <Input
              className="w-32 text-right"
              value={amountYuan}
              onChange={(event) => setAmountYuan(event.target.value)}
              required
            />
          </Field>

          <Button type="submit" disabled={!ready || createBudget.isPending}>
            {createBudget.isPending ? "提交中…" : "新增"}
          </Button>
        </form>

        <p className="mt-3 text-xs text-muted">
          填 1000 元 → 请求体发 <code className="font-mono">100000</code>（分）→
          库里也是 <code className="font-mono">100000</code>。
        </p>
        {localError && (
          <p className="mt-3 rounded-sm border border-danger/30 bg-danger-bg px-3 py-2 text-sm text-danger">
            {localError}
          </p>
        )}
        <ErrorNote error={createBudget.error} />
      </Card>

      <Card title="预算列表">
        {budgets.isPending && <Loading />}
        <ErrorNote error={budgets.error} />

        {budgets.data && budgets.data.length === 0 && (
          <EmptyState>还没有预算。</EmptyState>
        )}

        {budgets.data && budgets.data.length > 0 && (
          <Table
            head={[
              "部门",
              "品类",
              "年度",
              <span className="block text-right">总额</span>,
              <span className="block text-right">已预留</span>,
              <span className="block text-right">已花</span>,
              <span className="block text-right">可用</span>,
            ]}
          >
            {budgets.data.map((budget) => {
              const available =
                budget.amount_total_fen -
                budget.amount_reserved_fen -
                budget.amount_spent_fen;
              return (
                <Row key={budget.id}>
                  <Cell>{departmentName.get(budget.department_id) ?? "—"}</Cell>
                  <Cell>{categoryName.get(budget.category_id) ?? "—"}</Cell>
                  <Cell className="text-muted">{budget.year}</Cell>
                  <Cell className="text-right font-mono">
                    {formatFen(budget.amount_total_fen)}
                  </Cell>
                  <Cell className="text-right font-mono text-muted">
                    {formatFen(budget.amount_reserved_fen)}
                  </Cell>
                  <Cell className="text-right font-mono text-muted">
                    {formatFen(budget.amount_spent_fen)}
                  </Cell>
                  <Cell
                    className={
                      "text-right font-mono " +
                      (available > 0 ? "text-success" : "text-danger")
                    }
                  >
                    <span title={`${fenToYuan(available)} 元`}>
                      {formatFen(available)}
                    </span>
                  </Cell>
                </Row>
              );
            })}
          </Table>
        )}
      </Card>
    </>
  );
}

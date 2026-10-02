/** 采购品类与**审批规则编辑器**（M4 重刷样式）。
 *
 * 编辑器只做"下拉选角色 + 填金额阈值 + 增删行"，由前端拼成策略 JSON ——
 * 第一版刻意不上 JSON 编辑器。
 *
 * 两个必须守住的点：
 *   1. 界面填「元」，请求体发「分」。换算只走 `lib/money.ts`。
 *   2. 后端 422 的 message **原样显示**。语义校验会返回
 *      "规则引用了未知字段 'amount'……" 这种带字段名的提示，
 *      那就是管理员唯一能自救的线索，不能用"保存失败"盖掉。
 */

import { useState, type FormEvent } from "react";

import type { ApprovalPolicy, ApprovalRole, Category } from "../../api/client";
import {
  useAdminCategories,
  useCreateCategory,
  useUpdateApprovalPolicy,
} from "../../api/hooks/useCatalog";
import { fenToYuan, yuanToFen } from "../../lib/money";
import {
  Button,
  Card,
  EmptyState,
  ErrorNote,
  Field,
  Input,
  Loading,
  PageHeader,
  Select,
} from "../../components/ui";

const ROLE_OPTIONS: { value: ApprovalRole; label: string }[] = [
  { value: "DEPT_MANAGER", label: "部门主管" },
  { value: "DEPT_HEAD", label: "部门负责人" },
  { value: "PROCUREMENT", label: "采购专员" },
  { value: "FINANCE", label: "财务" },
  { value: "GM", label: "总经理" },
];

const ROLE_LABEL: Record<string, string> = Object.fromEntries(
  ROLE_OPTIONS.map((option) => [option.value, option.label]),
);

type Row = { role: ApprovalRole; thresholdYuan: string };

const DEFAULT_ROWS: Row[] = [{ role: "DEPT_MANAGER", thresholdYuan: "0" }];

/** 只认"每一级都是 amount_fen >= 阈值"这种简单形状；否则返回 null，不让表单编辑。 */
function rowsFromPolicy(policy: ApprovalPolicy): Row[] | null {
  const steps = policy.steps ?? [];
  const rows: Row[] = [];
  for (const step of steps) {
    const gte = (step.when as { gte?: unknown }).gte;
    if (
      !Array.isArray(gte) ||
      gte.length !== 2 ||
      gte[0] !== "amount_fen" ||
      typeof gte[1] !== "number"
    ) {
      return null;
    }
    rows.push({ role: step.role, thresholdYuan: fenToYuan(gte[1]) });
  }
  return rows.length > 0 ? rows : null;
}

function buildPolicy(rows: Row[]): ApprovalPolicy {
  return {
    steps: rows.map((row, index) => ({
      order: (index + 1) * 10,
      role: row.role,
      mode: "SINGLE" as const,
      // 界面是「元」，这里换成「分」——全项目唯一允许做这个换算的地方
      when: { gte: ["amount_fen", yuanToFen(row.thresholdYuan)] },
    })),
    skip_when_framework_contract: false,
  };
}

export default function AdminCategories() {
  const categories = useAdminCategories();
  const createCategory = useCreateCategory();
  const updatePolicy = useUpdateApprovalPolicy();

  const [name, setName] = useState("");
  const [code, setCode] = useState("");
  const [rows, setRows] = useState<Row[]>(DEFAULT_ROWS);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);

  const mutation = editingId ? updatePolicy : createCategory;

  function resetForm() {
    setName("");
    setCode("");
    setRows(DEFAULT_ROWS);
    setEditingId(null);
    setLocalError(null);
    createCategory.reset();
    updatePolicy.reset();
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setLocalError(null);

    let policy: ApprovalPolicy;
    try {
      policy = buildPolicy(rows);
    } catch (error) {
      // yuanToFen 对非法金额抛错，而不是静默当 0 —— 静默把错误金额存进库更危险
      setLocalError((error as Error).message);
      return;
    }

    if (editingId) {
      updatePolicy.mutate({ id: editingId, policy }, { onSuccess: resetForm });
    } else {
      createCategory.mutate(
        { name: name.trim(), code: code.trim(), approval_policy: policy },
        { onSuccess: resetForm },
      );
    }
  }

  function startEdit(category: Category) {
    const loaded = rowsFromPolicy(category.approval_policy);
    if (!loaded) {
      setLocalError(
        `品类 ${category.code} 的审批策略不是"每一级都是 amount_fen >= 阈值"的简单形状，` +
          "表单无法编辑它（请用 API 或直接改库）。",
      );
      return;
    }
    setEditingId(category.id);
    setName(category.name);
    setCode(category.code);
    setRows(loaded);
    setLocalError(null);
  }

  return (
    <>
      <PageHeader
        title="采购品类"
        description="审批层级按金额动态生成，规则存在品类上：改规则不改代码。"
      />

      <Card title={editingId ? "修改审批策略" : "新增采购品类"}>
        <form onSubmit={handleSubmit} className="space-y-5">
          <div className="flex flex-wrap items-end gap-4">
            <Field label="名称">
              <Input
                value={name}
                onChange={(event) => setName(event.target.value)}
                required
                maxLength={200}
                disabled={editingId !== null}
              />
            </Field>
            <Field label="编码">
              <Input
                className="font-mono"
                value={code}
                onChange={(event) => setCode(event.target.value)}
                required
                maxLength={64}
                placeholder="CAT-OFFICE"
                disabled={editingId !== null}
              />
            </Field>
          </div>

          <div>
            <p className="text-sm font-medium text-body">审批规则</p>
            <p className="mt-1 text-xs text-muted">
              金额单位是「元」，提交时会转成「分」。
              <strong className="text-warning">第一级必须是 0 元</strong>
              （任何金额都要命中，否则某些金额区间会没有人审）。
            </p>

            <div className="mt-3 space-y-2">
              {rows.map((row, index) => (
                <div key={index} className="flex items-center gap-2">
                  <span className="w-6 text-right text-xs text-faint">
                    {index + 1}
                  </span>
                  <Select
                    value={row.role}
                    onChange={(event) =>
                      setRows(
                        rows.map((item, i) =>
                          i === index
                            ? { ...item, role: event.target.value as ApprovalRole }
                            : item,
                        ),
                      )
                    }
                  >
                    {ROLE_OPTIONS.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))}
                  </Select>
                  <span className="text-sm text-muted">金额 ≥</span>
                  <Input
                    className="w-28 text-right"
                    value={row.thresholdYuan}
                    onChange={(event) =>
                      setRows(
                        rows.map((item, i) =>
                          i === index
                            ? { ...item, thresholdYuan: event.target.value }
                            : item,
                        ),
                      )
                    }
                  />
                  <span className="text-sm text-muted">元</span>
                  <button
                    type="button"
                    disabled={rows.length === 1}
                    onClick={() => setRows(rows.filter((_, i) => i !== index))}
                    className="text-xs text-muted hover:text-danger disabled:opacity-30"
                  >
                    删除
                  </button>
                </div>
              ))}
            </div>

            <button
              type="button"
              onClick={() =>
                setRows([...rows, { role: "DEPT_HEAD", thresholdYuan: "1000" }])
              }
              className="mt-3 text-sm text-body hover:underline"
            >
              + 添加一级
            </button>
          </div>

          <div className="flex items-center gap-3">
            <Button type="submit" disabled={mutation.isPending}>
              {mutation.isPending ? "提交中…" : editingId ? "保存策略" : "新增"}
            </Button>
            {editingId && (
              <button
                type="button"
                onClick={resetForm}
                className="text-sm text-muted hover:underline"
              >
                取消
              </button>
            )}
          </div>
        </form>

        {localError && (
          <p className="mt-3 whitespace-pre-wrap rounded-sm border border-danger/30 bg-danger-bg px-3 py-2 text-sm text-danger">
            {localError}
          </p>
        )}
        <ErrorNote error={mutation.error} />
      </Card>

      <Card title="采购品类">
        {categories.isPending && <Loading />}
        <ErrorNote error={categories.error} />

        {categories.data && categories.data.length === 0 && (
          <EmptyState>还没有品类。</EmptyState>
        )}

        {categories.data && categories.data.length > 0 && (
          <ul className="space-y-3">
            {categories.data.map((category) => (
              <li
                key={category.id}
                className="rounded-sm border border-line bg-white p-4 text-sm"
              >
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <span className="font-mono text-muted">{category.code}</span>
                    <span className="mx-2 text-faint">|</span>
                    <span className="text-ink">{category.name}</span>
                    <span className="ml-2 text-xs text-muted">
                      版本 {category.version} ·{" "}
                      {category.approval_policy.steps.length} 级
                    </span>
                  </div>
                  <button
                    onClick={() => startEdit(category)}
                    className="shrink-0 text-sm text-body hover:underline"
                  >
                    编辑策略
                  </button>
                </div>
                <ol className="mt-2 space-y-0.5 text-xs text-muted">
                  {category.approval_policy.steps.map((step) => {
                    const gte = (step.when as { gte?: unknown[] }).gte;
                    const threshold =
                      Array.isArray(gte) && typeof gte[1] === "number"
                        ? `${fenToYuan(gte[1])} 元`
                        : JSON.stringify(step.when);
                    return (
                      <li key={step.order}>
                        {step.order}. {ROLE_LABEL[step.role] ?? step.role} · 金额 ≥{" "}
                        {threshold} · {step.mode === "ALL" ? "会签" : "或签"}
                      </li>
                    );
                  })}
                </ol>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </>
  );
}

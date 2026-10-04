/** 新建采购申请：选品类 → 动态表单 → 明细行 → 保存草稿（M5）。
 *
 * 三段顺序是有意与数据模型对齐的：
 *   1. **选品类** —— 品类的 `form_schema` 决定第 2 段渲染哪些字段；
 *   2. **动态表单** —— 按 schema 渲染，不写死字段（"改规则不改代码"在界面上的对应物）；
 *   3. **明细行** —— 金额一律界面填「元」、提交转「分」，换算只走 `lib/money.ts`。
 *
 * 一处刻意的省略：**金额不可手填**。它由明细算出来（后端重算），
 * 界面给一个只读的合计，让人看着自己填的行对不对。
 */

import { useMemo, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";

import type { Category, ItemInput, RequestUrgency } from "../../api/client";
import { useAdminCategories, useCategories } from "../../api/hooks/useCatalog";
import { useCreateRequest, useReplaceItems } from "../../api/hooks/useRequests";
import { useMe } from "../../api/hooks/useAuth";
import { fenToYuan, formatFen, yuanToFen } from "../../lib/money";
import {
  Button,
  Card,
  ErrorNote,
  Field,
  Input,
  PageHeader,
  Select,
} from "../../components/ui";

type FormSchema = {
  properties?: Record<string, { type?: string; title?: string; format?: string }>;
  required?: string[];
};

/** 一行明细的界面态。金额用「元」文本存着，提交时才换算。 */
type LineRow = { name: string; spec: string; quantity: string; unitPriceYuan: string };

const EMPTY_LINE: LineRow = { name: "", spec: "", quantity: "1", unitPriceYuan: "" };

/** 按 `form_schema` 渲染动态字段（只支持 string / number / date 三类，够 M5 用）。 */
function DynamicFields({
  schema,
  values,
  onChange,
}: {
  schema: FormSchema;
  values: Record<string, string>;
  onChange: (key: string, value: string) => void;
}) {
  const properties = schema.properties ?? {};
  const required = new Set(schema.required ?? []);
  const entries = Object.entries(properties);

  if (entries.length === 0) {
    return (
      <p className="text-sm text-muted">
        这个品类没有配置动态表单字段，直接填明细即可。
      </p>
    );
  }

  return (
    <div className="flex flex-wrap gap-4">
      {entries.map(([key, field]) => (
        <Field
          key={key}
          label={field.title ?? key}
          hint={required.has(key) ? "必填" : undefined}
        >
          <Input
            type={field.format === "date" ? "date" : field.type === "number" ? "number" : "text"}
            value={values[key] ?? ""}
            onChange={(event) => onChange(key, event.target.value)}
            required={required.has(key)}
          />
        </Field>
      ))}
    </div>
  );
}

export default function RequestNew() {
  const me = useMe();
  // 管理员看到的品类更全（含停用）；普通用户走公开列表
  const isAdmin = me.data?.roles.includes("ADMIN") ?? false;
  const adminCategories = useAdminCategories();
  const publicCategories = useCategories();
  const categoriesQuery = isAdmin ? adminCategories : publicCategories;

  // ============================ 没有部门的账号 ============================
  // 部门是**结构性必需**的：它决定这笔钱从哪条预算出、审批链找谁（`DEPT_*` 按部门含子部门解析）、
  // 以及谁能看见这张单子。所以后端在建草稿时就会 422（`NO_DEPARTMENT`）。
  //
  // **主防线在路由**：`router.tsx` 给 `/requests*` 加了 `RequireAuth requireDepartment`，
  // 没有部门的账号根本进不到这个页面（侧边栏也不显示「我的申请」）。
  // 下面这段是**兜底**：万一将来守卫被改掉或漏挂，这里仍然会说清原因，
  // 而不是让人填完一整张表才被后端拒。
  const meLoaded = me.isSuccess;
  const hasNoDepartment = meLoaded && !me.data?.department_id;

  const createDraft = useCreateRequest();
  const replaceItems = useReplaceItems();
  const navigate = useNavigate();

  const [categoryId, setCategoryId] = useState("");
  const [title, setTitle] = useState("");
  const [reason, setReason] = useState("");
  const [urgency, setUrgency] = useState<RequestUrgency>("NORMAL");
  const [formValues, setFormValues] = useState<Record<string, string>>({});
  const [lines, setLines] = useState<LineRow[]>([{ ...EMPTY_LINE }]);
  const [localError, setLocalError] = useState<string | null>(null);

  const categories: Category[] = categoriesQuery.data ?? [];
  const selected: Category | undefined = useMemo(
    () => categories.find((item) => item.id === categoryId),
    [categories, categoryId],
  );

  /** 界面上的合计（元）。**只是给人看的**：真正落库的金额由后端按明细重算。 */
  const previewTotalFen = useMemo(() => {
    let total = 0;
    for (const line of lines) {
      try {
        total += Number(line.quantity || "0") * yuanToFen(line.unitPriceYuan || "0");
      } catch {
        // 还没填完的行不参与合计（真正提交时会在后端被逐行校验并给出具体行号）
        return total;
      }
    }
    return total;
  }, [lines]);

  function updateLine(index: number, patch: Partial<LineRow>) {
    setLines((current) =>
      current.map((line, i) => (i === index ? { ...line, ...patch } : line)),
    );
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setLocalError(null);

    // 没部门的账号不用往后走了 —— 后端一定会 422，这里提前拦下并说清原因。
    // （前端拦只是体验：真正的边界仍然在后端。）
    if (hasNoDepartment) {
      setLocalError(
        "当前账号没有所属部门，无法创建采购申请。" +
          "请管理员在账号设置里指定所属部门；若这个账号本来就不该发起采购，请用需要采购的部门账号提交。",
      );
      return;
    }

    let items: ItemInput[];
    try {
      items = lines.map((line) => ({
        name: line.name.trim(),
        spec: line.spec.trim() || null,
        quantity: Number(line.quantity),
        // 界面「元」→ 请求体「分」：全项目唯一允许做这个换算的地方
        unit_price_fen: yuanToFen(line.unitPriceYuan),
      }));
    } catch (error) {
      setLocalError((error as Error).message);
      return;
    }

    if (items.length === 0) {
      setLocalError("至少要有 1 行明细");
      return;
    }

    // 先建草稿，再把明细整批写进去：金额由后端按明细重算，这里只是把内容递过去
    const draft = await createDraft.mutateAsync({
      category_id: categoryId,
      title: title.trim(),
      reason: reason.trim() || null,
      urgency,
      form_data: formValues,
    });
    await replaceItems.mutateAsync({
      id: draft.id,
      version: draft.version,
      items,
    });
    navigate(`/requests/${draft.id}`);
  }

  return (
    <>
      <PageHeader
        title="新建采购申请"
        description="先选品类（决定要填哪些字段），再填明细。保存后是草稿，确认无误再提交。"
        breadcrumb={[
          { label: "首页", to: "/" },
          { label: "我的申请", to: "/requests" },
          { label: "新建申请" },
        ]}
      />

      <Card>
        {/* 没有部门时**在表单最上方**就说清楚，别让人填完一整张表才被拒 */}
        {hasNoDepartment && (
          <p
            className="mb-4 rounded-sm border border-danger/30 bg-danger-bg px-3 py-2 text-sm text-danger"
            data-testid="no-department-notice"
          >
            当前账号没有所属部门，<b>无法创建采购申请</b>。
            部门决定这笔钱从哪条预算出、审批链找谁，所以它是必填项。
            请联系管理员给这个账号指定所属部门；如果这个账号本来就不该发起采购
            （例如系统管理员），请改用需要采购的部门账号提交。
          </p>
        )}
        <form onSubmit={handleSubmit} className="space-y-6">
          <div className="flex flex-wrap items-end gap-4">
            <Field label="采购品类">
              <Select
                value={categoryId}
                onChange={(event) => {
                  setCategoryId(event.target.value);
                  // 换品类 = 换一套表单字段，旧的填值不能带过去
                  setFormValues({});
                }}
                required
              >
                <option value="">请选择</option>
                {categories.map((category) => (
                  <option key={category.id} value={category.id}>
                    {category.name}
                  </option>
                ))}
              </Select>
            </Field>

            <Field label="标题" className="min-w-64 flex-1">
              <Input
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                required
                maxLength={200}
                placeholder="例如：行政部 10 月办公用品"
              />
            </Field>

            <Field label="紧急程度">
              <Select
                value={urgency}
                onChange={(event) => setUrgency(event.target.value as RequestUrgency)}
              >
                <option value="NORMAL">普通</option>
                <option value="URGENT">加急</option>
              </Select>
            </Field>
          </div>

          <div>
            <p className="text-sm font-medium text-body">申请说明</p>
            <textarea
              className="mt-1 w-full rounded-sm border border-line-strong bg-white px-2 py-1.5 text-sm text-ink"
              rows={2}
              maxLength={2000}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              placeholder="为什么要买（可选）"
            />
          </div>

          {selected && (
            <div>
              <p className="text-sm font-medium text-body">
                {selected.name} · 品类表单
              </p>
              <div className="mt-2">
                <DynamicFields
                  schema={(selected.form_schema ?? {}) as FormSchema}
                  values={formValues}
                  onChange={(key, value) =>
                    setFormValues((current) => ({ ...current, [key]: value }))
                  }
                />
              </div>
            </div>
          )}

          <div>
            <div className="flex items-baseline justify-between">
              <p className="text-sm font-medium text-body">明细</p>
              <p className="text-sm text-muted">
                合计（由后端重算）：
                <span className="ml-1 font-mono text-ink">
                  {formatFen(previewTotalFen)}
                </span>
              </p>
            </div>

            <div className="mt-2 space-y-2">
              {lines.map((line, index) => (
                <div key={index} className="flex flex-wrap items-end gap-2">
                  <Field label="名称" className="min-w-40 flex-1">
                    <Input
                      value={line.name}
                      onChange={(event) => updateLine(index, { name: event.target.value })}
                      required
                      maxLength={200}
                    />
                  </Field>
                  <Field label="规格" className="min-w-32">
                    <Input
                      value={line.spec}
                      onChange={(event) => updateLine(index, { spec: event.target.value })}
                      maxLength={500}
                    />
                  </Field>
                  <Field label="数量" className="w-24">
                    <Input
                      className="text-right"
                      type="number"
                      min={1}
                      value={line.quantity}
                      onChange={(event) =>
                        updateLine(index, { quantity: event.target.value })
                      }
                      required
                    />
                  </Field>
                  <Field label="单价（元）" className="w-32">
                    <Input
                      className="text-right"
                      value={line.unitPriceYuan}
                      onChange={(event) =>
                        updateLine(index, { unitPriceYuan: event.target.value })
                      }
                      placeholder="0.00"
                      required
                    />
                  </Field>
                  <button
                    type="button"
                    disabled={lines.length === 1}
                    onClick={() => setLines(lines.filter((_, i) => i !== index))}
                    className="pb-2 text-xs text-muted hover:text-danger disabled:opacity-30"
                  >
                    删除
                  </button>
                </div>
              ))}
            </div>

            <button
              type="button"
              onClick={() => setLines([...lines, { ...EMPTY_LINE }])}
              className="mt-3 text-sm text-body hover:underline"
            >
              + 加一行
            </button>

            <p className="mt-2 text-xs text-faint">
              界面填「元」，请求体发「分」（例：{fenToYuan(100_000)} 元 → 100000）。
            </p>
          </div>

          <div className="flex items-center gap-3">
            <Button
              type="submit"
              disabled={createDraft.isPending || replaceItems.isPending || hasNoDepartment}
            >
              {createDraft.isPending || replaceItems.isPending ? "保存中…" : "保存草稿"}
            </Button>
            <button
              type="button"
              onClick={() => navigate("/requests")}
              className="text-sm text-muted hover:underline"
            >
              {hasNoDepartment ? "返回我的申请" : "取消"}
            </button>
          </div>
        </form>

        {localError && (
          <p className="mt-3 whitespace-pre-wrap rounded-sm border border-danger/30 bg-danger-bg px-3 py-2 text-sm text-danger">
            {localError}
          </p>
        )}
        <ErrorNote error={createDraft.error ?? replaceItems.error} />
      </Card>
    </>
  );
}

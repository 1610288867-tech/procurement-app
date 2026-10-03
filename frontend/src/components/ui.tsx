/** 界面原语（M4 设计系统）。
 *
 * 这里只放"每个页面都要用、而且必须长得一样"的东西：卡片、表单字段、按钮、
 * 表格、状态徽标、错误提示。
 *
 * ============================ 为什么要有这一层 ============================
 * 之前每个页面各写各的 `className`：三个管理页的输入框边框色不完全一致、
 * 错误提示的红有好几种、表格间距靠手感。改一次风格就要改六处，而且总会漏一处。
 *
 * 现在颜色的**取值**在 `index.css` 的 `@theme` 里（换肤只动那一个文件），
 * 用法的**约定**在这里（页面不直接写颜色，只选组件）。
 * 一条纪律：**后端 422 的 message 必须原样显示**。`ErrorNote` 只负责排版，
 * 绝不改写、绝不吞掉 `request_id` —— 那是管理员唯一能自救的线索。
 */

import type {
  ButtonHTMLAttributes,
  InputHTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
} from "react";

import { ApiError } from "../api/client";

// ------------------------------------------------------------------ 容器

/** 卡片：界面里最主要的分区容器。 */
export function Card({
  title,
  action,
  children,
  className = "",
}: {
  title?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={`rounded-md border border-line bg-card p-6 shadow-[0_1px_2px_rgba(23,23,23,0.04)] ${className}`}
    >
      {(title || action) && (
        <div className="mb-4 flex items-baseline justify-between gap-4">
          {title && (
            <h2 className="text-lg font-semibold text-ink">{title}</h2>
          )}
          {action}
        </div>
      )}
      {children}
    </section>
  );
}

/** 页面标题区：左侧标题 + 右侧操作。 */
export function PageHeader({
  title,
  description,
  action,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <header className="flex items-start justify-between gap-4 border-b border-line pb-4">
      <div>
        <h1 className="text-xl font-semibold text-ink">{title}</h1>
        {description && (
          <p className="mt-1 text-sm text-muted">{description}</p>
        )}
      </div>
      {action}
    </header>
  );
}

// ------------------------------------------------------------------ 表单

/** 字段容器：标签在上，控件在下，宽度可调。 */
export function Field({
  label,
  hint,
  children,
  className = "",
}: {
  label: string;
  hint?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <label className={`flex flex-col gap-1 text-sm ${className}`}>
      <span className="text-muted">{label}</span>
      {children}
      {hint && <span className="text-xs text-faint">{hint}</span>}
    </label>
  );
}

export const inputClass =
  "rounded-sm border border-line-strong bg-white px-2 py-1.5 text-sm text-ink " +
  "placeholder:text-faint disabled:bg-section disabled:text-faint";

export function Input(props: InputHTMLAttributes<HTMLInputElement>) {
  const { className = "", ...rest } = props;
  return <input {...rest} className={`${inputClass} ${className}`} />;
}

export function Select(props: SelectHTMLAttributes<HTMLSelectElement>) {
  const { className = "", ...rest } = props;
  return <select {...rest} className={`${inputClass} ${className}`} />;
}

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "danger" | "ghost";
};

export function Button({
  variant = "primary",
  className = "",
  ...rest
}: ButtonProps) {
  const styles = {
    primary: "bg-primary text-primary-fg hover:bg-primary-hover",
    danger: "bg-danger text-white hover:opacity-90",
    ghost:
      "border border-line-strong bg-white text-body hover:bg-hover",
  }[variant];
  return (
    <button
      {...rest}
      className={
        "rounded-sm px-4 py-1.5 text-sm font-medium disabled:opacity-50 " +
        `${styles} ${className}`
      }
    />
  );
}

// ------------------------------------------------------------------ 反馈

/** 把后端错误原样展示；带 request_id 时一并给出，方便对日志。 */
export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null;
  const message = (error as Error).message;
  const requestId = error instanceof ApiError ? error.requestId : undefined;
  return (
    <p className="mt-3 whitespace-pre-wrap rounded-sm border border-danger/30 bg-danger-bg px-3 py-2 text-sm text-danger">
      {message}
      {requestId && (
        <span className="ml-1 text-faint">（request_id: {requestId}）</span>
      )}
    </p>
  );
}

/** 次要提示（不是错误，只是说明）。 */
export function HintNote({ children }: { children: ReactNode }) {
  return (
    <p className="rounded-sm border border-warning/30 bg-warning-bg px-3 py-2 text-sm text-warning">
      {children}
    </p>
  );
}

export function Loading({ label = "加载中…" }: { label?: string }) {
  return <p className="text-sm text-muted">{label}</p>;
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <p className="text-sm text-muted">{children}</p>;
}

/** 状态徽标。绿色=在用，灰色=停用；不引入第三、第四种颜色。 */
export function StatusBadge({ active }: { active: boolean }) {
  return (
    <span
      className={
        "rounded-sm px-2 py-0.5 text-xs " +
        (active
          ? "bg-success-bg text-success"
          : "bg-section text-muted")
      }
    >
      {active ? "启用" : "停用"}
    </span>
  );
}

/** 采购申请状态的中文名与配色（M5 建，M6 补两个）。
 *
 * 六种状态用六种观感，但只从既有语义色里取：草稿中性、审批中偏提示、
 * 撤回/丢弃是终态但不代表出错（所以用中性灰而不是红色）。
 * **红色只留给"被驳回"**（M6）—— 那才是需要人立刻处理的；
 * `SOURCING`（审批通过、进入询价）用成功色，它是这条申请第一次"往前走"。
 */
const REQUEST_STATUS_STYLE: Record<string, { label: string; className: string }> = {
  DRAFT: { label: "草稿", className: "bg-section text-muted" },
  IN_APPROVAL: { label: "审批中", className: "bg-warning-bg text-warning" },
  SOURCING: { label: "询价中", className: "bg-success-bg text-success" },
  REJECTED: { label: "已驳回", className: "bg-danger-bg text-danger" },
  WITHDRAWN: { label: "已撤回", className: "bg-section text-faint" },
  DISCARDED: { label: "已丢弃", className: "bg-section text-faint" },
};

export function RequestStatusBadge({ status }: { status: string }) {
  const style = REQUEST_STATUS_STYLE[status];
  return (
    <span
      className={
        "rounded-sm px-2 py-0.5 text-xs " +
        (style?.className ?? "bg-section text-muted")
      }
    >
      {style?.label ?? status}
    </span>
  );
}

// ------------------------------------------------------------------ 表格

export function Table({
  head,
  children,
}: {
  head: ReactNode[];
  children: ReactNode;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-sm">
        <thead className="border-b border-line text-muted">
          <tr>
            {head.map((cell, index) => (
              <th key={index} className="py-2 pr-4 font-medium">
                {cell}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

/** 表格行：统一的底部分隔线。 */
export function Row({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <tr className={`border-b border-line/60 last:border-0 ${className}`}>
      {children}
    </tr>
  );
}

export function Cell({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return <td className={`py-2 pr-4 text-body ${className}`}>{children}</td>;
}

// ------------------------------------------------------------------ 审批（M6）

/** 审批任务状态 → 中文与配色。
 *
 * 词表里**没有"还没轮到"**：那件事由审批流的 `current_step_order` 表达，
 * 界面上显示成时间轴的"未开始 ○"是算出来的（见 `ApprovalTimeline`）。
 */
const TASK_STATUS_STYLE: Record<string, { label: string; className: string }> = {
  PENDING: { label: "待处理", className: "bg-warning-bg text-warning" },
  APPROVED: { label: "已通过", className: "bg-success-bg text-success" },
  REJECTED: { label: "已驳回", className: "bg-danger-bg text-danger" },
  SKIPPED: { label: "已跳过", className: "bg-section text-faint" },
  CANCELLED: { label: "已取消", className: "bg-section text-faint" },
};

export function TaskStatusBadge({ status }: { status: string }) {
  const style = TASK_STATUS_STYLE[status];
  return (
    <span
      className={
        "rounded-sm px-2 py-0.5 text-xs " +
        (style?.className ?? "bg-section text-muted")
      }
    >
      {style?.label ?? status}
    </span>
  );
}

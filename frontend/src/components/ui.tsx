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
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { ApiError } from "../api/client";

// ------------------------------------------------------------------ 容器

/** 卡片：界面里最主要的分区容器。
 *
 * `id` 是给"本页导航"用的锚点（`PageToc`）—— 长页面（申请详情）靠它做页内跳转，
 * 这是参考站（文档站 / 博客）里最实用的一个排版手法：内容长到要滚动时，
 * 右侧给一条目录，读者不必来回滚。
 */
export function Card({
  title,
  description,
  action,
  children,
  className = "",
  id,
}: {
  title?: string;
  description?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
  id?: string;
}) {
  return (
    <section
      id={id}
      className={
        "scroll-mt-24 rounded-lg border border-line bg-card p-6 shadow-card " + className
      }
    >
      {(title || action) && (
        <div className="mb-4 flex items-baseline justify-between gap-4">
          {title && (
            <div>
              <h2 className="text-lg font-semibold text-ink">{title}</h2>
              {description && (
                <p className="mt-1 text-xs text-muted">{description}</p>
              )}
            </div>
          )}
          {action}
        </div>
      )}
      {children}
    </section>
  );
}

/** 面包屑的一节：给了 `to` 就能点回上级。 */
export type Crumb = { label: string; to?: string };

/** 页面标题区：面包屑 + 标题 + 说明 + 右侧操作。
 *
 * 面包屑是"我在哪一层"的答案（参考站的做法：`您的位置：首页 > …`）。
 * 它**只反映导航层级**，不掺业务状态 —— 状态有状态徽标，两件事不要混在一行里。
 */
export function PageHeader({
  title,
  description,
  action,
  breadcrumb,
}: {
  title: string;
  description?: string;
  action?: ReactNode;
  breadcrumb?: Crumb[];
}) {
  return (
    <header className="border-b border-line pb-4">
      {breadcrumb && breadcrumb.length > 0 && (
        // 只在窄屏显示：宽屏上面有粘性顶栏，那里已经有一条同样的面包屑
        // （顶栏在窄屏是隐藏的，所以这一条是移动端的兜底）—— 两条同时出现就是噪音。
        <nav
          aria-label="面包屑"
          className="mb-2 flex flex-wrap items-center gap-1 text-xs md:hidden"
        >
          {breadcrumb.map((crumb, index) => (
            <span key={crumb.label + index} className="flex items-center gap-1">
              {index > 0 && <span className="text-faint">/</span>}
              {crumb.to ? (
                <Link to={crumb.to} className="text-muted hover:text-ink">
                  {crumb.label}
                </Link>
              ) : (
                <span className="text-body">{crumb.label}</span>
              )}
            </span>
          ))}
        </nav>
      )}
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-xl font-semibold text-ink">{title}</h1>
          {description && (
            <p className="mt-1 text-sm text-muted">{description}</p>
          )}
        </div>
        {action && <div className="shrink-0">{action}</div>}
      </div>
    </header>
  );
}

/** 指标卡：一个大数字 + 一句标签（工作台、看板用）。
 *
 * 为什么单独做成原语：数字要等宽（`tabular-nums`）才能上下对齐 ——
 * 这是"看起来专业"和"看起来像凑的"之间最便宜的一条分界线。
 */
export function Stat({
  label,
  value,
  hint,
  to,
  tone = "default",
}: {
  label: string;
  value: ReactNode;
  hint?: string;
  to?: string;
  tone?: "default" | "success" | "warning" | "danger";
}) {
  const toneClass = {
    default: "text-ink",
    success: "text-success",
    warning: "text-warning",
    danger: "text-danger",
  }[tone];

  const body = (
    <>
      <p className="text-xs text-muted">{label}</p>
      <p className={`mt-1 font-mono text-2xl tabular-nums ${toneClass}`}>{value}</p>
      {hint && <p className="mt-1 text-xs text-faint">{hint}</p>}
    </>
  );

  const shell =
    "block rounded-lg border border-line bg-card px-4 py-3 shadow-card transition-colors";
  return to ? (
    <Link to={to} className={`${shell} hover:border-line-strong hover:bg-section/60`}>
      {body}
    </Link>
  ) : (
    <div className={shell}>{body}</div>
  );
}

/** 本页导航（右侧粘性目录）。
 *
 * 长页面（申请详情：内容 / 明细 / 审批进度 / 时间线）最缺的就是"我现在读到哪、
 * 还剩什么"，参考站的文档页与博客都靠右侧目录解决。
 *
 * **条目由调用方按"实际渲染出来的分区"给** —— 目录里出现一个页面上没有的锚点，
 * 比没有目录更糟（点过去什么都不动）。草稿没有审批流，所以草稿就不该有那一条。
 */
export function PageToc({
  items,
  title = "本页导航",
}: {
  items: { id: string; label: string }[];
  title?: string;
}) {
  const [active, setActive] = useState(items[0]?.id ?? "");
  // 依赖用"id 列表"而不是数组本身：调用方多半是内联构造的数组，
  // 每次渲染都是新引用，直接依赖它会让观察器每帧重挂一次。
  const key = items.map((item) => item.id).join(",");

  useEffect(() => {
    if (typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((entry) => entry.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
        if (visible?.target.id) setActive(visible.target.id);
      },
      // 顶部留出粘性顶栏的高度，底部收窄：这样"当前分区"是视觉上正在读的那一段
      { rootMargin: "-88px 0px -60% 0px", threshold: 0 },
    );
    for (const id of key.split(",")) {
      const element = id ? document.getElementById(id) : null;
      if (element) observer.observe(element);
    }
    return () => observer.disconnect();
  }, [key]);

  if (items.length === 0) return null;

  return (
    <nav
      aria-label={title}
      data-testid="page-toc"
      className="sticky top-24 hidden rounded-lg border border-line bg-card p-3 shadow-card lg:block"
    >
      <p className="mb-2 px-2 text-xs text-faint">{title}</p>
      <ul className="space-y-0.5">
        {items.map((item) => (
          <li key={item.id}>
            <a
              href={`#${item.id}`}
              className={
                "block rounded-sm px-2 py-1 text-sm transition-colors " +
                (active === item.id
                  ? "bg-section font-medium text-ink"
                  : "text-muted hover:bg-hover hover:text-ink")
              }
            >
              {item.label}
            </a>
          </li>
        ))}
      </ul>
    </nav>
  );
}

/** 通用小徽标（加急、类型标签等）。语义色仍是既有那四组。 */
export function Badge({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "neutral" | "success" | "warning" | "danger";
}) {
  const toneClass = {
    neutral: "bg-section text-muted",
    success: "bg-success-bg text-success",
    warning: "bg-warning-bg text-warning",
    danger: "bg-danger-bg text-danger",
  }[tone];
  return (
    <span className={`rounded-sm px-2 py-0.5 text-xs ${toneClass}`}>{children}</span>
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
  "rounded-md border border-line-strong bg-white px-3 py-2 text-sm text-ink " +
  "transition-colors placeholder:text-faint hover:border-faint " +
  "focus:border-primary focus:outline-none " +
  "disabled:bg-section disabled:text-faint";

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
  /** `block` 占满整行（登录按钮、表单主操作）；`sm` 是表格行内的小按钮。 */
  size?: "sm" | "md";
  block?: boolean;
};

export function Button({
  variant = "primary",
  size = "md",
  block = false,
  className = "",
  ...rest
}: ButtonProps) {
  const styles = {
    primary: "bg-primary text-primary-fg hover:bg-primary-hover",
    danger: "bg-danger text-white hover:opacity-90",
    ghost: "border border-line-strong bg-white text-body hover:bg-hover",
  }[variant];
  const sizes = {
    sm: "px-2.5 py-1 text-xs",
    md: "px-4 py-2 text-sm",
  }[size];
  return (
    <button
      {...rest}
      className={
        "rounded-md font-medium transition-colors disabled:opacity-50 " +
        `${sizes} ${styles} ${block ? "w-full" : ""} ${className}`
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
    <p className="rounded-md border border-warning/30 bg-warning-bg px-3 py-2 text-sm text-warning">
      {children}
    </p>
  );
}

export function Loading({ label = "加载中…" }: { label?: string }) {
  return <p className="text-sm text-muted">{label}</p>;
}

/** 空状态：居中的一句话，可选一个"接下来该做什么"的入口。
 *
 * 空列表最忌讳只写"暂无数据" —— 用户真正的疑问是"那我该干什么"。
 */
export function EmptyState({
  children,
  action,
}: {
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center gap-3 rounded-md border border-dashed border-line px-6 py-8 text-center">
      <p className="text-sm text-muted">{children}</p>
      {action}
    </div>
  );
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

/** 采购申请状态的中文名与配色（M5 建，M6 补两个，M10 补三个履约状态）。
 *
 * 六种状态用六种观感，但只从既有语义色里取：草稿中性、审批中偏提示、
 * 撤回/丢弃是终态但不代表出错（所以用中性灰而不是红色）。
 * **红色只留给"被驳回"**（M6）—— 那才是需要人立刻处理的；
 * `SOURCING`（审批通过、进入询价）用成功色，它是这条申请第一次"往前走"。
 *
 * M10 的三个履约状态都沿用成功色：它们都是"事情在往前走"，
 * 与 `SOURCING` 同一类观感 —— 配色不为了区分而区分，**要区分的是"要不要人处理"**。
 * （不加这三行的话，徽章会原样显示 `AWARDED` 这样的英文码：`?? status` 是兜底，
 *   它保证不崩，但不保证好看。）
 */
const REQUEST_STATUS_STYLE: Record<string, { label: string; className: string }> = {
  DRAFT: { label: "草稿", className: "bg-section text-muted" },
  IN_APPROVAL: { label: "审批中", className: "bg-warning-bg text-warning" },
  SOURCING: { label: "询价中", className: "bg-success-bg text-success" },
  AWARDED: { label: "已定标", className: "bg-success-bg text-success" },
  ORDERED: { label: "已下单", className: "bg-success-bg text-success" },
  RECEIVED: { label: "已收货", className: "bg-success-bg text-success" },
  INVOICED: { label: "已收票", className: "bg-success-bg text-success" },
  PAID: { label: "已付款", className: "bg-success-bg text-success" },
  CLOSED: { label: "已归档", className: "bg-section text-muted" },
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
        <thead className="border-b border-line text-xs text-muted">
          <tr>
            {head.map((cell, index) => (
              <th key={index} className="whitespace-nowrap py-2 pr-4 font-medium">
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

/** 表格行：统一的底部分隔线 + 悬停高亮。
 *
 * 悬停高亮不是装饰：待办与申请列表一行有六七列、还要横向扫金额，
 * 没有行高亮时很容易看串行。
 */
export function Row({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <tr
      className={
        "border-b border-line/60 transition-colors last:border-0 hover:bg-section/50 " +
        className
      }
    >
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
  return <td className={`py-2.5 pr-4 text-body ${className}`}>{children}</td>;
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

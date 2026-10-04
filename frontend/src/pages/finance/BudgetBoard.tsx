/** 预算看板（M7）—— **只读**。
 *
 * ============================ 这一页解决什么问题 ============================
 * 钱花在哪、还占着多少、还能花多少 —— 财务在批付款之前要看的就是这三个数。
 * 之前的 `/admin/budgets` 只有管理员能开，而"看钱"是财务的本职
 * （内控指引第 7 号把"审核采购预算"列为付款前置），所以 M7 单开一个只读看板：
 *
 *     看钱 = FINANCE + ADMIN（这一页）
 *     改钱 = 只走 /admin/budgets（ADMIN）
 *
 * 两个动作分开，才能分别收紧 —— 混成一个接口就只能取权限的并集。
 *
 * ============================ 金额一律「分」 ============================
 * 接口给的是 `*_fen`，界面上的「元」只经 `lib/money.ts` 换算。
 * "可用"由三个字段相减得出，**不在前端另发明公式**：
 * 后端 `domain/budget_policy.available_fen` 是那条公式的唯一落点。
 */

import { useMemo } from "react";

import { useBoardBudgets, useBudgetTransactions } from "../../api/hooks/useCatalog";
import type { BudgetTransactionKind } from "../../api/client";
import { fenToYuan, formatFen } from "../../lib/money";
import {
  Card,
  Cell,
  EmptyState,
  ErrorNote,
  PageHeader,
  Row,
  Table,
} from "../../components/ui";

/** 流水类型的中文名与配色。三类合起来必须能还原预算的每一次变动。 */
const KIND_LABEL: Record<BudgetTransactionKind, string> = {
  RESERVE: "预留",
  RELEASE: "释放",
  SPEND: "结转",
};

const KIND_CLASS: Record<BudgetTransactionKind, string> = {
  RESERVE: "bg-hover text-muted",
  RELEASE: "bg-success-bg text-success",
  SPEND: "bg-danger-bg text-danger",
};

/** 低于这个比例就标黄：留给财务的反应时间，不是业务规则。 */
const LOW_RATIO = 0.2;

function when(value: string): string {
  return value.slice(0, 16).replace("T", " ");
}

function UsageBar({ ratio }: { ratio: number }) {
  const percent = Math.min(100, Math.max(0, ratio * 100));
  const tone =
    ratio <= 0 ? "bg-danger" : ratio < LOW_RATIO ? "bg-warning" : "bg-success";
  return (
    <div className="flex items-center gap-2">
      <div className="h-1.5 w-24 overflow-hidden rounded-full bg-hover">
        <div className={"h-full " + tone} style={{ width: `${percent}%` }} />
      </div>
      <span className="font-mono text-xs text-muted">{percent.toFixed(0)}%</span>
    </div>
  );
}

export default function BudgetBoard() {
  const budgets = useBoardBudgets();
  const transactions = useBudgetTransactions();

  const rows = useMemo(() => {
    return (budgets.data ?? []).map((budget) => {
      const available =
        budget.amount_total_fen -
        budget.amount_reserved_fen -
        budget.amount_spent_fen;
      return {
        ...budget,
        available_fen: available,
        used_ratio:
          budget.amount_total_fen > 0
            ? (budget.amount_reserved_fen + budget.amount_spent_fen) /
              budget.amount_total_fen
            : 0,
      };
    });
  }, [budgets.data]);

  const totals = rows.reduce(
    (acc, row) => ({
      total: acc.total + row.amount_total_fen,
      reserved: acc.reserved + row.amount_reserved_fen,
      spent: acc.spent + row.amount_spent_fen,
      available: acc.available + row.available_fen,
    }),
    { total: 0, reserved: 0, spent: 0, available: 0 },
  );

  return (
    <>
      <PageHeader
        title="预算看板"
        description="部门 × 品类的总额 / 已预留 / 已花 / 可用。只读 —— 调整预算请走「预算」管理页。"
      />

      <Card title="合计">
        <ErrorNote error={budgets.error} />
        {budgets.isPending && <EmptyState>加载中…</EmptyState>}
        {budgets.data && (
          <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
            {[
              ["总额", totals.total],
              ["已预留", totals.reserved],
              ["已花", totals.spent],
              ["可用", totals.available],
            ].map(([label, value]) => (
              <div key={label as string}>
                <p className="text-xs text-muted">{label}</p>
                <p
                  className={
                    "font-mono text-lg " +
                    (label === "可用"
                      ? (value as number) > 0
                        ? "text-success"
                        : "text-danger"
                      : "text-ink")
                  }
                  title={`${fenToYuan(value as number)} 元`}
                >
                  {formatFen(value as number)}
                </p>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card title={`预算（${rows.length}）`}>
        {budgets.data && rows.length === 0 && (
          <EmptyState>还没有预算。请先在「预算」管理页配置部门 × 品类的额度。</EmptyState>
        )}

        {rows.length > 0 && (
          <Table
            head={[
              "部门",
              "品类",
              "年度",
              <span className="block text-right">总额</span>,
              <span className="block text-right">已预留</span>,
              <span className="block text-right">已花</span>,
              <span className="block text-right">可用</span>,
              "使用率",
            ]}
          >
            {rows.map((row) => (
              <Row key={row.id}>
                <Cell>{row.department_name}</Cell>
                <Cell>{row.category_name}</Cell>
                <Cell className="text-muted">{row.year}</Cell>
                <Cell className="text-right font-mono">{formatFen(row.amount_total_fen)}</Cell>
                <Cell className="text-right font-mono text-muted">
                  {formatFen(row.amount_reserved_fen)}
                </Cell>
                <Cell className="text-right font-mono text-muted">
                  {formatFen(row.amount_spent_fen)}
                </Cell>
                <Cell
                  className={
                    "text-right font-mono " +
                    (row.available_fen > 0 ? "text-success" : "text-danger")
                  }
                >
                  {formatFen(row.available_fen)}
                </Cell>
                <Cell>
                  <UsageBar ratio={row.used_ratio} />
                </Cell>
              </Row>
            ))}
          </Table>
        )}

        <p className="mt-3 text-xs text-muted">
          「可用」= 总额 − 已预留 − 已花。可用为 0 时新的申请会被拒绝（422），
          需要先调整预算再重新提交。
        </p>
      </Card>

      <Card title={`预算流水（最近 ${transactions.data?.length ?? 0} 条）`}>
        <ErrorNote error={transactions.error} />
        {transactions.isPending && <EmptyState>加载中…</EmptyState>}

        {transactions.data && transactions.data.length === 0 && (
          <EmptyState>还没有流水。提交一笔申请后就会出现 RESERVE。</EmptyState>
        )}

        {transactions.data && transactions.data.length > 0 && (
          <Table
            head={[
              "时间",
              "单号",
              "类型",
              <span className="block text-right">金额</span>,
            ]}
          >
            {transactions.data.map((item) => (
              <Row key={item.id}>
                <Cell className="whitespace-nowrap text-muted">{when(item.created_at)}</Cell>
                <Cell className="font-mono text-xs">{item.request_code}</Cell>
                <Cell>
                  <span
                    className={
                      "rounded-sm px-1.5 py-0.5 text-xs " + KIND_CLASS[item.kind]
                    }
                  >
                    {KIND_LABEL[item.kind]} {item.kind}
                  </span>
                </Cell>
                <Cell className="text-right font-mono">{formatFen(item.amount_fen)}</Cell>
              </Row>
            ))}
          </Table>
        )}

        <p className="mt-3 text-xs text-muted">
          预留（提交）− 释放（驳回 / 撤回，以及"预留 − 实付"的差额）− 结转（付款）
          = 0：这三类流水加起来必须归零，否则说明有一笔钱没放回去。
        </p>
      </Card>
    </>
  );
}

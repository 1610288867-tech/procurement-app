/** 全生命周期事件时间线（M9）。
 *
 * ============================ 它和 ApprovalTimeline 不是一回事 ============================
 * 两个组件看起来都是时间轴，但**数据来源与回答的问题完全不同**：
 *
 * | 组件 | 数据来源 | 回答的问题 |
 * | --- | --- | --- |
 * | `ApprovalTimeline` | `approval_tasks` + `policy_snapshot` | 这笔申请**卡在谁那里**、还有几级没走 |
 * | `RequestTimeline`（本文件） | `request_events` | 这笔申请**经历过什么**（含上传附件、将来的询价/收货/付款） |
 *
 * 前者会随任务状态变化（是"当前进度视图"），后者**只追加**（是"历史流水"）。
 * 混成一个组件的话，M10 想加"付款完成"这种事件就无处安放 ——
 * 它不是审批链上的一级。所以这里各管各的。
 *
 * ============================ 顺序由后端保证 ============================
 * 后端按 `request_events.seq`（数据库发的单调序列）正序返回，**不按时间戳** ——
 * 同一事务里写的多条事件时间戳可能相同，按时间排会退化成随机顺序（M8 修过）。
 * 所以这里**就地渲染、不自己排序**：前端再排一次等于把这个保证复制一份，
 * 两份排序早晚会不一致。
 */

import type { RequestEvent } from "../api/client";

/** 事件类型 → 中文（词表在 `backend/app/domain/events.py`）。
 *
 * 认不出的一律**原样显示英文码**，不显示"未知事件"：
 * 排查问题时那个英文码才是线索（而且它同时说明"有人绕过了常量表"）。
 */
export const EVENT_LABEL: Record<string, string> = {
  REQUEST_CREATED: "创建草稿",
  REQUEST_UPDATED: "修改草稿",
  REQUEST_SUBMITTED: "提交",
  REQUEST_WITHDRAWN: "撤回",
  REQUEST_DISCARDED: "丢弃",
  APPROVAL_STARTED: "生成审批链",
  APPROVAL_CLAIMED: "认领审批任务",
  APPROVAL_APPROVED: "审批通过",
  APPROVAL_REJECTED: "审批驳回",
  APPROVAL_COMPLETED: "审批全部通过",
  ATTACHMENT_ADDED: "上传附件",
};

const STATUS_LABEL: Record<string, string> = {
  DRAFT: "草稿",
  IN_APPROVAL: "审批中",
  SOURCING: "询价中",
  REJECTED: "已驳回",
  WITHDRAWN: "已撤回",
  DISCARDED: "已丢弃",
};

function statusLabel(status: string): string {
  return STATUS_LABEL[status] ?? status;
}

function formatTime(raw: string): string {
  // 后端给的是 UTC ISO 串；这里只做"看得懂"的处理，不引日期库
  return raw.slice(0, 19).replace("T", " ");
}

function formatSize(bytes: unknown): string | null {
  if (typeof bytes !== "number" || bytes <= 0) return null;
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/** 事件附带的一句话说明（来自 `payload` 快照）。
 *
 * 事件表刻意**不设外键到明细**：明细会被覆盖式替换，事件必须留下当时的快照。
 * 这里读的就是那份快照，所以历史事件的说明不会随数据变化而改写 ——
 * 这正是"只追加"的价值。
 */
function describe(event: RequestEvent): string | null {
  const payload = event.payload ?? {};
  if (event.event_type === "ATTACHMENT_ADDED") {
    const name = typeof payload.filename === "string" ? payload.filename : "附件";
    const size = formatSize(payload.size_bytes);
    return size ? `${name}（${size}）` : name;
  }
  return null;
}

export function RequestTimeline({
  events,
  /** 空列表时的说明。草稿与已提交的单子看到的空状态不一样，由调用方决定措辞。 */
  emptyHint = "还没有事件。",
}: {
  events: RequestEvent[];
  emptyHint?: string;
}) {
  if (events.length === 0) {
    return <p className="text-sm text-muted">{emptyHint}</p>;
  }

  return (
    <ol className="space-y-0" data-testid="request-timeline">
      {events.map((event, index) => {
        const changed = event.from_status !== event.to_status;
        const detail = describe(event);
        return (
          <li key={event.id} className="flex gap-3">
            {/* 左侧：圆点 + 连接线（最后一项不收线） */}
            <div className="flex flex-col items-center">
              <span
                className={
                  "mt-1 h-2 w-2 shrink-0 rounded-full " +
                  (changed ? "bg-primary" : "bg-line")
                }
                aria-hidden="true"
              />
              {index < events.length - 1 && (
                <span className="w-px flex-1 bg-line" aria-hidden="true" />
              )}
            </div>

            <div className="min-w-0 flex-1 pb-4">
              <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                <span className="text-sm text-ink">
                  {EVENT_LABEL[event.event_type] ?? event.event_type}
                </span>
                {/* 只有**真的**状态变化才显示 from → to。
                    APPROVAL_STARTED 与 ATTACHMENT_ADDED 的 from == to，
                    给它们画一个 "IN_APPROVAL → IN_APPROVAL" 只会让人困惑。 */}
                {changed && (
                  <span className="text-xs text-muted">
                    {event.from_status ? statusLabel(event.from_status) : "（初始）"}
                    {" → "}
                    {event.to_status ? statusLabel(event.to_status) : "（无）"}
                  </span>
                )}
              </div>
              {detail && <p className="mt-0.5 truncate text-xs text-body">{detail}</p>}
              <p className="mt-0.5 font-mono text-xs text-faint">
                {formatTime(event.created_at)}
              </p>
            </div>
          </li>
        );
      })}
    </ol>
  );
}

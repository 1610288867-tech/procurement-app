/** 申请详情：内容 + 明细 + 审批进度 + 附件 + 时间线（M5；M9 起加附件与事件流水）。
 *
 * 三个按钮的出现条件是**状态决定的**（草稿可改可提交，审批中只能撤回）——
 * 但真正拦住的永远是后端：前端藏按钮只是别让人点了白点。
 *
 * ============================ M9：两个时间轴是分开的 ============================
 * 「审批进度」读 `approval_tasks`（卡片 + `ApprovalTimeline`，回答"卡在谁那里"）；
 * 「时间线」读 `request_events`（`RequestTimeline`，回答"经历过什么"）。
 * 数据来源不同、语义不同，所以是两个组件、两个分区 —— 详见 `RequestTimeline` 的文件头。
 */

import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import type { ItemInput } from "../../api/client";
import { useRequestFlow } from "../../api/hooks/useApprovals";
import { useRequestDetail } from "../../api/hooks/useRequests";
import {
  useDiscardRequest,
  usePatchRequest,
  useReplaceItems,
  useSubmitRequest,
  useWithdrawRequest,
} from "../../api/hooks/useRequests";
import { formatFen, yuanToFen } from "../../lib/money";
import { newIdempotencyKey } from "../../lib/idempotency";
import { ApprovalTimeline } from "../../components/ApprovalTimeline";
import { AttachmentSection } from "../../components/AttachmentSection";
import { RequestTimeline } from "../../components/RequestTimeline";

/** 履约链上的状态（M10）：这些情况下这张单子已经进了"审批通过之后"的流程，
 *  详情页要给一个跳去履约工作台的入口。**与后端状态机同源** ——
 *  `SOURCING` 之前的单子没有履约可做，给入口只会让人点进去看到空工作台。 */
const FULFILLMENT_STATUSES = [
  "SOURCING",
  "AWARDED",
  "ORDERED",
  "RECEIVED",
  "INVOICED",
  "PAID",
  "CLOSED",
];
import {
  Button,
  Card,
  Cell,
  ErrorNote,
  Field,
  HintNote,
  Input,
  Loading,
  PageHeader,
  PageToc,
  RequestStatusBadge,
  Row,
  Table,
} from "../../components/ui";

type LineRow = { name: string; spec: string; quantity: string; unitPriceYuan: string };

export default function RequestDetail() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const detail = useRequestDetail(id || null);
  // 审批进度（M6）：草稿没有审批流，所以这个查询会 404 —— 由 useRequestFlow 关掉重试
  const flow = useRequestFlow(id || null);

  const patch = usePatchRequest();
  const replaceItems = useReplaceItems();
  const submit = useSubmitRequest();
  const withdraw = useWithdrawRequest();
  const discard = useDiscardRequest();

  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState("");
  const [reason, setReason] = useState("");
  const [lines, setLines] = useState<LineRow[]>([]);
  const [submitKey] = useState(() => newIdempotencyKey());
  const [localError, setLocalError] = useState<string | null>(null);

  const request = detail.data?.request;

  function startEditing() {
    if (!detail.data) return;
    setTitle(detail.data.request.title);
    setReason(detail.data.request.reason ?? "");
    setLines(
      detail.data.items.map((item) => ({
        name: item.name,
        spec: item.spec ?? "",
        quantity: String(item.quantity),
        unitPriceYuan: (item.unit_price_fen / 100).toFixed(2),
      })),
    );
    setLocalError(null);
    setEditing(true);
  }

  async function saveEdits() {
    if (!request) return;
    setLocalError(null);

    let items: ItemInput[];
    try {
      items = lines.map((line) => ({
        name: line.name.trim(),
        spec: line.spec.trim() || null,
        quantity: Number(line.quantity),
        unit_price_fen: yuanToFen(line.unitPriceYuan),
      }));
    } catch (error) {
      setLocalError((error as Error).message);
      return;
    }

    try {
      const patched = await patch.mutateAsync({
        id: request.id,
        version: request.version,
        title: title.trim(),
        reason: reason.trim() || null,
      });
      // 明细整批覆盖：金额与明细在同一个事务里更新，题面不会和明细对不上
      await replaceItems.mutateAsync({
        id: request.id,
        version: patched.version,
        items,
      });
      setEditing(false);
    } catch {
      // 错误已经落在 mutation.error 里，由 ErrorNote 原样展示（含后端 409/422 原文）
    }
  }

  if (detail.isPending) {
    return (
      <Card>
        <Loading />
      </Card>
    );
  }

  if (detail.isError || !detail.data || !request) {
    return (
      <Card>
        <ErrorNote error={detail.error} />
        <p className="text-sm text-muted">
          看不到这张申请（可能不存在，或者不在你的可见范围内）。
        </p>
        <Link to="/requests" className="mt-3 inline-block text-sm text-body hover:underline">
          ← 回到列表
        </Link>
      </Card>
    );
  }

  return (
    <>
      <PageHeader
        title={request.title}
        description={`${request.code} · ${formatFen(request.amount_total_fen)}`}
        action={<RequestStatusBadge status={request.status} />}
        breadcrumb={[
          { label: "首页", to: "/" },
          { label: "我的申请", to: "/requests" },
          { label: "申请详情" },
        ]}
      />

      {/* 两栏：正文 + 右侧「本页导航」。
          这一页有四段（内容 / 明细 / 审批进度 / 时间线），滚到中段很容易忘记下面还有什么。
          窄屏没有右侧栏，TOC 直接不渲染 —— 它只是导航辅助，不该在手机上占一整屏。

          ⚠️ 目录条目按**实际渲染出来的分区**生成：草稿没有审批流，就不该有「审批进度」
          这一条 —— 点过去什么都不动的目录比没有目录更糟。 */}
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_180px]">
        <div className="min-w-0 space-y-6">
          <Card title="申请内容" id="section-content">
            {!editing ? (
              <div className="space-y-4">
                <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm">
                  <Detail label="紧急程度" value={request.urgency === "URGENT" ? "加急" : "普通"} />
                  <Detail label="金额" value={formatFen(request.amount_total_fen)} mono />
                  <Detail label="创建时间" value={request.created_at.slice(0, 19).replace("T", " ")} />
                  <Detail
                    label="提交时间"
                    value={
                      request.submitted_at
                        ? request.submitted_at.slice(0, 19).replace("T", " ")
                        : "—"
                    }
                  />
                </dl>
                {request.reason && (
                  <p className="text-sm text-body">说明：{request.reason}</p>
                )}
                {Object.keys(request.form_data ?? {}).length > 0 && (
                  <div className="text-sm">
                    <p className="text-muted">品类表单</p>
                    <pre className="mt-1 overflow-x-auto rounded-md bg-section p-3 text-xs text-body">
                      {JSON.stringify(request.form_data, null, 2)}
                    </pre>
                  </div>
                )}
              </div>
            ) : (
              <div className="space-y-4">
                <Field label="标题">
                  <Input
                    value={title}
                    onChange={(event) => setTitle(event.target.value)}
                    required
                    maxLength={200}
                  />
                </Field>
                <Field label="说明">
                  <Input
                    value={reason}
                    onChange={(event) => setReason(event.target.value)}
                    maxLength={2000}
                  />
                </Field>

                <div>
                  <p className="text-sm font-medium text-body">明细</p>
                  <div className="mt-2 space-y-2">
                    {lines.map((line, index) => (
                      <div key={index} className="flex flex-wrap items-end gap-2">
                        <Field label="名称" className="min-w-40 flex-1">
                          <Input
                            value={line.name}
                            onChange={(event) =>
                              setLines((current) =>
                                current.map((item, i) =>
                                  i === index ? { ...item, name: event.target.value } : item,
                                ),
                              )
                            }
                            required
                          />
                        </Field>
                        <Field label="规格" className="min-w-32">
                          <Input
                            value={line.spec}
                            onChange={(event) =>
                              setLines((current) =>
                                current.map((item, i) =>
                                  i === index ? { ...item, spec: event.target.value } : item,
                                ),
                              )
                            }
                          />
                        </Field>
                        <Field label="数量" className="w-24">
                          <Input
                            className="text-right"
                            type="number"
                            min={1}
                            value={line.quantity}
                            onChange={(event) =>
                              setLines((current) =>
                                current.map((item, i) =>
                                  i === index
                                    ? { ...item, quantity: event.target.value }
                                    : item,
                                ),
                              )
                            }
                            required
                          />
                        </Field>
                        <Field label="单价（元）" className="w-32">
                          <Input
                            className="text-right"
                            value={line.unitPriceYuan}
                            onChange={(event) =>
                              setLines((current) =>
                                current.map((item, i) =>
                                  i === index
                                    ? { ...item, unitPriceYuan: event.target.value }
                                    : item,
                                ),
                              )
                            }
                            required
                          />
                        </Field>
                        <button
                          type="button"
                          disabled={lines.length === 1}
                          onClick={() =>
                            setLines((current) => current.filter((_, i) => i !== index))
                          }
                          className="pb-2 text-xs text-muted hover:text-danger disabled:opacity-30"
                        >
                          删除
                        </button>
                      </div>
                    ))}
                  </div>
                  <button
                    type="button"
                    onClick={() =>
                      setLines([
                        ...lines,
                        { name: "", spec: "", quantity: "1", unitPriceYuan: "" },
                      ])
                    }
                    className="mt-3 text-sm text-body hover:underline"
                  >
                    + 加一行
                  </button>
                </div>

                <div className="flex items-center gap-3">
                  <Button
                    onClick={saveEdits}
                    disabled={patch.isPending || replaceItems.isPending}
                  >
                    {patch.isPending || replaceItems.isPending ? "保存中…" : "保存"}
                  </Button>
                  <button
                    type="button"
                    onClick={() => setEditing(false)}
                    className="text-sm text-muted hover:underline"
                  >
                    取消
                  </button>
                </div>
              </div>
            )}

            {!editing && (
              <div className="mt-5 flex items-center gap-3 border-t border-line pt-4">
                {request.status === "DRAFT" && (
                  <>
                    <Button variant="ghost" onClick={startEditing}>
                      编辑草稿
                    </Button>
                    <Button
                      disabled={submit.isPending}
                      onClick={() =>
                        submit.mutate({
                          id: request.id,
                          version: request.version,
                          idempotencyKey: submitKey,
                        })
                      }
                    >
                      提交审批
                    </Button>
                    <button
                      className="text-sm text-muted hover:text-danger"
                      disabled={discard.isPending}
                      onClick={() =>
                        discard.mutate(
                          { id: request.id, version: request.version },
                          { onSuccess: () => navigate("/requests") },
                        )
                      }
                    >
                      丢弃
                    </button>
                  </>
                )}
                {request.status === "IN_APPROVAL" && (
                  <Button
                    variant="ghost"
                    disabled={withdraw.isPending}
                    onClick={() =>
                      withdraw.mutate({
                        id: request.id,
                        version: request.version,
                        idempotencyKey: newIdempotencyKey(),
                      })
                    }
                  >
                    撤回
                  </Button>
                )}
                {(request.status === "WITHDRAWN" || request.status === "DISCARDED") && (
                  <p className="text-sm text-muted">这张申请已经结束，不能再改动。</p>
                )}
              </div>
            )}

            {localError && (
              <p className="mt-3 whitespace-pre-wrap rounded-md border border-danger/30 bg-danger-bg px-3 py-2 text-sm text-danger">
                {localError}
              </p>
            )}
            <ErrorNote
              error={
                patch.error ?? replaceItems.error ?? submit.error ?? withdraw.error ?? discard.error
              }
            />
          </Card>

          <Card
            title="明细"
            id="section-items"
            description={`${detail.data.items.length} 行 · 金额由后端按明细重算，客户端传的总额不作数`}
          >
            <Table head={["#", "名称", "规格", "数量", "单价", "金额"]}>
              {detail.data.items.map((item) => (
                <Row key={item.id}>
                  <Cell className="text-muted">{item.line_no}</Cell>
                  <Cell>{item.name}</Cell>
                  <Cell className="text-muted">{item.spec ?? "—"}</Cell>
                  <Cell className="text-right font-mono">{item.quantity}</Cell>
                  <Cell className="text-right font-mono">{formatFen(item.unit_price_fen)}</Cell>
                  <Cell className="text-right font-mono">{formatFen(item.amount_fen)}</Cell>
                </Row>
              ))}
            </Table>
            {detail.data.items.length === 0 && (
              <p className="text-sm text-muted">还没有明细，点「编辑草稿」加几行。</p>
            )}
          </Card>

          {/* 审批链进度（M6）：申请人也看得到 —— "卡在谁那里"是申请人最想知道的一件事。
              草稿还没有审批流，后端会 404，所以只在提交过之后才查（见 useRequestFlow 的 retry:false）。 */}
          {request && request.status !== "DRAFT" && (
            <Card
              title="审批进度"
              id="section-approval"
              description="依据的是提交那一刻的策略快照 —— 管理员后来改过阈值，也不会改变已经提交的单子。"
            >
              {flow.data ? (
                <ApprovalTimeline
                  steps={flow.data.steps}
                  currentStepOrder={flow.data.current_step_order}
                />
              ) : (
                <p className="text-sm text-muted">
                  {flow.isPending ? "加载中…" : "还没有审批链。"}
                </p>
              )}
              {flow.data && (
                <p className="mt-3 text-xs text-faint">
                  这一版策略是第 {flow.data.policy_version} 版。
                </p>
              )}
            </Card>
          )}

          {/* 履约链（M10）：审批通过之后这张单子会走到询价 / 订单 / 收货 / 发票 / 付款。
              这里只放一个**跳转入口**，不把整条链嵌进详情页 ——
              履约的每一步都有它自己的表单与角色（采购 / 收货 / 财务），
              嵌进来会让"谁能操作哪一步"在同一个页面里说不清。
              `?request=<id>` 让工作台一进去就展开这一张。 */}
          {FULFILLMENT_STATUSES.includes(request.status) && (
            <Card
              title="履约进度"
              id="section-fulfillment"
              description="询价定标 → 下单收货 → 收票匹配 → 付款归档，都在履约工作台里做。"
            >
              <div className="flex flex-wrap items-center gap-3 text-sm">
                <RequestStatusBadge status={request.status} />
                <Link
                  className="text-primary hover:underline"
                  to={`/fulfillment?request=${request.id}`}
                >
                  在履约工作台里打开这笔单子 →
                </Link>
              </div>
              <div className="mt-3">
                <HintNote>
                  能不能操作某一步取决于你的角色（采购 / 收货 / 财务），
                  以及这张单子当前走到哪一步。后端每一步都会独立校验，
                  这里只是把入口放在顺手的地方。
                </HintNote>
              </div>
            </Card>
          )}

          {/* 附件（M9）：报价单、合同、发票。草稿阶段也能传 —— 报价单往往
              在提交前就先拿到了，没有理由卡到提交之后。 */}
          <Card
            title="附件"
            id="section-attachments"
            description="PDF / JPG / PNG，单个不超过 10 MB。文件名与内容都会校验，改扩展名没有用。"
          >
            <AttachmentSection requestId={request.id} />
          </Card>

          <Card
            title="时间线"
            id="section-timeline"
            description={`${detail.data.timeline.length} 条事件 · 只追加，永不修改`}
          >
            <RequestTimeline events={detail.data.timeline} />
          </Card>
        </div>

        <PageToc
          items={[
            { id: "section-content", label: "申请内容" },
            { id: "section-items", label: `明细（${detail.data.items.length}）` },
            ...(request.status === "DRAFT"
              ? []
              : [{ id: "section-approval", label: "审批进度" }]),
            // 履约进度只在已经进链的单子上出现 —— 目录里挂一个点了没反应的条目
            // 比不给更糟（用户会以为页面坏了）
            ...(FULFILLMENT_STATUSES.includes(request.status)
              ? [{ id: "section-fulfillment", label: "履约进度" }]
              : []),
            { id: "section-attachments", label: "附件" },
            { id: "section-timeline", label: `时间线（${detail.data.timeline.length}）` },
          ]}
        />
      </div>
    </>
  );
}

function Detail({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div>
      <dt className="text-muted">{label}</dt>
      <dd className={mono ? "font-mono text-ink" : "text-ink"}>{value}</dd>
    </div>
  );
}

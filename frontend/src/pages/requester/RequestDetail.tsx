/** 申请详情：内容 + 明细 + 时间线（M5）。
 *
 * 三个按钮的出现条件是**状态决定的**（草稿可改可提交，审批中只能撤回）——
 * 但真正拦住的永远是后端：前端藏按钮只是别让人点了白点。
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
import {
  Button,
  Card,
  Cell,
  ErrorNote,
  Field,
  Input,
  Loading,
  PageHeader,
  RequestStatusBadge,
  Row,
  Table,
} from "../../components/ui";

/** 事件类型 → 中文（词表在 `backend/app/domain/events.py`）。 */
const EVENT_LABEL: Record<string, string> = {
  REQUEST_CREATED: "创建草稿",
  REQUEST_UPDATED: "修改草稿",
  REQUEST_SUBMITTED: "提交",
  REQUEST_WITHDRAWN: "撤回",
  REQUEST_DISCARDED: "丢弃",
  // M6：审批链上的动作。APPROVAL_STARTED 不是状态变更（from/to 都是 IN_APPROVAL），
  // 它记的是"按当时的策略算出了哪几级"这个事实。
  APPROVAL_STARTED: "生成审批链",
  APPROVAL_CLAIMED: "认领审批任务",
  APPROVAL_APPROVED: "审批通过",
  APPROVAL_REJECTED: "审批驳回",
  APPROVAL_COMPLETED: "审批全部通过",
};

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
      />

      <Card title="申请内容">
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
                <pre className="mt-1 overflow-x-auto rounded-sm bg-section p-3 text-xs text-body">
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
          <p className="mt-3 whitespace-pre-wrap rounded-sm border border-danger/30 bg-danger-bg px-3 py-2 text-sm text-danger">
            {localError}
          </p>
        )}
        <ErrorNote
          error={
            patch.error ?? replaceItems.error ?? submit.error ?? withdraw.error ?? discard.error
          }
        />
      </Card>

      <Card title="明细">
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
        <Card title="审批进度">
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
              这份进度依据的是<b>提交那一刻</b>的策略（第 {flow.data.policy_version} 版）——
              管理员后来改过阈值，也不会改变已经提交的单子。
            </p>
          )}
        </Card>
      )}

      <Card title="时间线">
        <ol className="space-y-3">
          {detail.data.timeline.map((event) => (
            <li key={event.id} className="flex gap-3 text-sm">
              <span className="w-36 shrink-0 font-mono text-xs text-faint">
                {event.created_at.slice(0, 19).replace("T", " ")}
              </span>
              <span className="text-body">
                {EVENT_LABEL[event.event_type] ?? event.event_type}
                {event.from_status && event.to_status && (
                  <span className="ml-2 text-xs text-muted">
                    {event.from_status} → {event.to_status}
                  </span>
                )}
              </span>
            </li>
          ))}
        </ol>
        {detail.data.timeline.length === 0 && (
          <p className="text-sm text-muted">还没有事件。</p>
        )}
      </Card>
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

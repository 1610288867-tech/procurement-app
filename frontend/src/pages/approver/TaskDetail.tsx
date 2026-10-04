/** 审批任务详情：申请全貌 + 审批链 + 通过/驳回（M6）。
 *
 * 三件事必须出现在这一页上，缺一件审批人就做不了判断：
 *
 * 1. **申请全貌**（标题、品类、金额、申请人、加急、事由）—— 不看内容就点"通过"是耍流氓；
 * 2. **审批链全貌**（前面谁批过了、我这一级是第几级、后面还有几级）——
 *    审批人要能判断"这事有没有经过该经的人"；
 * 3. **通过 / 驳回**，且驳回**意见必填** —— 它是申请人唯一能看到的理由。
 *
 * ============================ 权限不是这一页说了算 ============================
 * 界面只对"当前这一级"显示按钮，但**真正拦住越级审批的是后端**：
 * 决定接口会把 `step_order = current_step_order` 写进 `WHERE`，
 * 不属于当前级一律 403（可见但还没轮到 → 403；不在可见范围 → 404）。
 */

import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { ApiError } from "../../api/client";
import {
  useApproveTask,
  useClaimTask,
  useRejectTask,
  useTaskDetail,
} from "../../api/hooks/useApprovals";
import { ApprovalTimeline, roleLabel } from "../../components/ApprovalTimeline";
import {
  Button,
  Card,
  Cell,
  ErrorNote,
  Field,
  Loading,
  PageHeader,
  RequestStatusBadge,
  Row,
  Table,
  TaskStatusBadge,
} from "../../components/ui";
import { formatFen } from "../../lib/money";

export default function TaskDetail() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const detail = useTaskDetail(id || null);

  const claim = useClaimTask();
  const approve = useApproveTask();
  const reject = useRejectTask();

  const [comment, setComment] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);

  if (detail.isPending) return <Loading label="加载审批任务…" />;
  if (detail.error) {
    return (
      <>
        <PageHeader
          title="审批任务"
          breadcrumb={[
            { label: "首页", to: "/" },
            { label: "我的待办", to: "/approvals/tasks" },
            { label: "审批任务" },
          ]}
        />
        <Card>
          <ErrorNote error={detail.error} />
          <p className="mt-2 text-xs text-faint">
            403 = 这张单子还没轮到这一级（或不是分配给您的）；404 = 任务不存在。
          </p>
          <Link to="/approvals/tasks" className="mt-4 inline-block text-sm text-body hover:underline">
            ← 回到我的待办
          </Link>
        </Card>
      </>
    );
  }
  if (!detail.data) return null;

  const { task, request, steps, current_step_order, assignee_name } = detail.data;
  const isCurrentStep = task.step_order === current_step_order;
  const canDecide = isCurrentStep && task.status === "PENDING";
  const needsClaim = canDecide && task.assignee_id === null && task.mode === "SINGLE";

  /** 409 = 别人抢先处理了（或版本过期）。提示刷新，不假装成功。 */
  function explain(error: unknown): string | null {
    if (!error) return null;
    if (error instanceof ApiError && error.status === 409) {
      return "这条任务刚刚被处理过了（可能是别人抢先一步）。列表已刷新，请重新查看。";
    }
    if (error instanceof ApiError && error.status === 403) {
      return "这张单子还没轮到这一级，或不是分配给您的。";
    }
    return null;
  }

  function onApprove() {
    setLocalError(null);
    approve.mutate(
      {
        id: task.id,
        version: task.version,
        comment: comment.trim() || null,
        requestId: request.id,
      },
      {
        onSuccess: (result) => {
          // 一级过完只是往下走；全部过完主单会离开 IN_APPROVAL
          if (result.request_status !== "IN_APPROVAL") {
            navigate(`/requests/${request.id}`);
          } else {
            navigate("/approvals/tasks");
          }
        },
        onError: (error) => setLocalError(explain(error)),
      },
    );
  }

  function onReject() {
    setLocalError(null);
    if (!comment.trim()) {
      setLocalError("驳回必须填写意见 —— 它是申请人唯一能看到的理由。");
      return;
    }
    reject.mutate(
      { id: task.id, version: task.version, comment: comment.trim(), requestId: request.id },
      {
        onSuccess: () => navigate("/approvals/tasks"),
        onError: (error) => setLocalError(explain(error)),
      },
    );
  }

  return (
    <>
      <PageHeader
        title={request.title}
        description={`${request.code} · ${request.category_name} · 提交于 ${
          request.submitted_at ? request.submitted_at.slice(0, 16).replace("T", " ") : "—"
        }`}
        action={
          <Link to="/approvals/tasks" className="text-sm text-body hover:underline">
            ← 回到待办
          </Link>
        }
        breadcrumb={[
          { label: "首页", to: "/" },
          { label: "我的待办", to: "/approvals/tasks" },
          { label: "审批任务" },
        ]}
      />

      <div className="grid gap-4 lg:grid-cols-[2fr_1fr]">
        <div className="space-y-4">
          <Card title="申请内容">
            <Table head={["项目", "值"]}>
              <Row>
                <Cell className="w-32 text-muted">标题</Cell>
                <Cell>{request.title}</Cell>
              </Row>
              <Row>
                <Cell className="text-muted">品类</Cell>
                <Cell>{request.category_name}</Cell>
              </Row>
              <Row>
                <Cell className="text-muted">金额</Cell>
                <Cell className="font-mono">{formatFen(request.amount_total_fen)}</Cell>
              </Row>
              <Row>
                <Cell className="text-muted">紧急程度</Cell>
                <Cell>
                  {request.urgency === "URGENT" ? (
                    <span className="rounded-sm bg-danger-bg px-1.5 py-0.5 text-xs text-danger">
                      加急
                    </span>
                  ) : (
                    "普通"
                  )}
                </Cell>
              </Row>
              <Row>
                <Cell className="text-muted">申请状态</Cell>
                <Cell>
                  <RequestStatusBadge status={request.status} />
                </Cell>
              </Row>
            </Table>
            <Link
              to={`/requests/${request.id}`}
              className="mt-4 inline-block text-sm text-body hover:underline"
            >
              查看完整申请（含明细与操作时间线）→
            </Link>
          </Card>

          <Card title="我的审批">
            <div className="mb-3 flex flex-wrap items-center gap-2 text-sm">
              <span className="font-medium text-ink">
                第 {task.step_order} 级 · {roleLabel(task.role)}
              </span>
              <TaskStatusBadge status={task.status} />
              {task.mode === "ALL" && (
                <span className="rounded-sm bg-section px-2 py-0.5 text-xs text-muted">
                  会签：本级所有人都要通过
                </span>
              )}
              {task.mode === "SINGLE" && (
                <span className="rounded-sm bg-section px-2 py-0.5 text-xs text-muted">
                  或签：本级任一人通过即可
                </span>
              )}
              {assignee_name && (
                <span className="text-xs text-muted">已由 {assignee_name} 认领</span>
              )}
            </div>

            <ErrorNote error={approve.error ?? reject.error ?? claim.error} />
            {localError && (
              <p className="mb-3 rounded-sm bg-danger-bg px-3 py-2 text-sm text-danger">
                {localError}
              </p>
            )}

            {!canDecide && (
              <p className="rounded-sm bg-section px-3 py-2 text-sm text-muted">
                {isCurrentStep
                  ? `这条任务已经处理过了（${task.status}）。`
                  : `这张单子现在在第 ${current_step_order} 级，还没轮到第 ${task.step_order} 级 —— 所以现在不能处理。`}
              </p>
            )}

            {canDecide && (
              <div className="space-y-3">
                <Field label="审批意见" hint="通过时可以留空；驳回时必填。">
                  <textarea
                    className="w-full rounded-sm border border-line-strong bg-card px-3 py-2 text-sm text-body outline-none focus:border-primary"
                    rows={3}
                    value={comment}
                    onChange={(event) => setComment(event.target.value)}
                    placeholder="写清依据，申请人会看到这段话"
                  />
                </Field>

                <div className="flex items-center gap-3">
                  {needsClaim ? (
                    <Button
                      disabled={claim.isPending}
                      onClick={() =>
                        claim.mutate(
                          { id: task.id, version: task.version, requestId: request.id },
                          {
                            onSuccess: () => setLocalError(null),
                            onError: (error) => setLocalError(explain(error)),
                          },
                        )
                      }
                    >
                      认领这条任务
                    </Button>
                  ) : (
                    <>
                      <Button disabled={approve.isPending} onClick={onApprove}>
                        通过
                      </Button>
                      <Button
                        variant="danger"
                        disabled={reject.isPending}
                        onClick={onReject}
                      >
                        驳回
                      </Button>
                    </>
                  )}
                </div>
                {needsClaim && (
                  <p className="text-xs text-faint">
                    或签任务先到先得：认领之后这条任务就归您，别人再点会被拒绝。
                  </p>
                )}
              </div>
            )}
          </Card>
        </div>

        <Card title="审批链进度">
          <ApprovalTimeline steps={steps} currentStepOrder={current_step_order} />
          <p className="mt-2 text-xs text-faint">
            只有当前这一级的任务会出现在审批人的待办里；未开始的级别不会提前可见。
          </p>
        </Card>
      </div>
    </>
  );
}

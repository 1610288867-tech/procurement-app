/** 我的采购申请：列表 + 状态筛选（M5）。
 *
 * 列表能看见哪些行**由后端按可见范围决定**（写进 WHERE）：
 * REQUESTER 只看得到自己的，部门角色看本部门含子部门，采购/财务/GM/管理员看全公司。
 * 前端这里不做任何过滤 —— 藏起来不等于没权限。
 */

import { useState } from "react";
import { Link } from "react-router-dom";

import type { RequestStatus } from "../../api/client";
import {
  useDiscardRequest,
  useRequests,
  useSubmitRequest,
  useWithdrawRequest,
} from "../../api/hooks/useRequests";
import { formatFen } from "../../lib/money";
import { newIdempotencyKey } from "../../lib/idempotency";
import {
  Button,
  Card,
  Cell,
  EmptyState,
  Loading,
  ErrorNote,
  PageHeader,
  RequestStatusBadge,
  Row,
  Select,
  Table,
} from "../../components/ui";

const STATUS_FILTERS: { value: RequestStatus | ""; label: string }[] = [
  { value: "", label: "全部" },
  { value: "DRAFT", label: "草稿" },
  { value: "IN_APPROVAL", label: "审批中" },
  { value: "SOURCING", label: "询价中" },
  { value: "REJECTED", label: "已驳回" },
  { value: "WITHDRAWN", label: "已撤回" },
  { value: "DISCARDED", label: "已丢弃" },
];

export default function RequestList() {
  const [status, setStatus] = useState<RequestStatus | "">("");
  const requests = useRequests(status === "" ? undefined : status);
  const submit = useSubmitRequest();
  const withdraw = useWithdrawRequest();
  const discard = useDiscardRequest();

  const rows = requests.data?.items ?? [];

  return (
    <>
      <PageHeader
        title="我的采购申请"
        description="草稿可以改；提交后进入审批，不能再改，但可以撤回。"
        breadcrumb={[{ label: "首页", to: "/" }, { label: "我的申请" }]}
        action={
          <Link to="/requests/new" className="text-sm text-body hover:underline">
            + 新建申请
          </Link>
        }
      />

      <Card
        title="申请列表"
        action={
          <Select
            value={status}
            onChange={(event) => setStatus(event.target.value as RequestStatus | "")}
            aria-label="按状态筛选"
          >
            {STATUS_FILTERS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </Select>
        }
      >
        <ErrorNote error={requests.error} />
        {requests.isPending && <Loading label="加载申请列表…" />}

        {requests.data && rows.length === 0 && (
          <EmptyState>
            还没有申请。点右上角「新建申请」建一张草稿试试。
          </EmptyState>
        )}

        {rows.length > 0 && (
          <Table
            head={["单号", "标题", "金额", "状态", "操作"]}
          >
            {rows.map((request) => (
              <Row key={request.id}>
                <Cell className="font-mono text-xs text-muted">{request.code}</Cell>
                <Cell>
                  <Link
                    to={`/requests/${request.id}`}
                    className="text-body hover:underline"
                  >
                    {request.title}
                  </Link>
                  {request.urgency === "URGENT" && (
                    <span className="ml-2 rounded-sm bg-danger-bg px-1.5 py-0.5 text-xs text-danger">
                      加急
                    </span>
                  )}
                </Cell>
                <Cell className="text-right font-mono">
                  {formatFen(request.amount_total_fen)}
                </Cell>
                <Cell>
                  <RequestStatusBadge status={request.status} />
                </Cell>
                <Cell>
                  <div className="flex items-center gap-2">
                    {request.status === "DRAFT" && (
                      <>
                        <Link
                          to={`/requests/${request.id}`}
                          className="text-sm text-body hover:underline"
                        >
                          继续编辑
                        </Link>
                        <Button
                          className="px-2 py-1 text-xs"
                          disabled={submit.isPending}
                          onClick={() =>
                            submit.mutate({
                              id: request.id,
                              version: request.version,
                              // 一次点击 = 一把新键；React Query 的重试会复用它
                              idempotencyKey: newIdempotencyKey(),
                            })
                          }
                        >
                          提交
                        </Button>
                        <button
                          className="text-xs text-muted hover:text-danger"
                          disabled={discard.isPending}
                          onClick={() =>
                            discard.mutate({ id: request.id, version: request.version })
                          }
                        >
                          丢弃
                        </button>
                      </>
                    )}
                    {request.status === "IN_APPROVAL" && (
                      <button
                        className="text-xs text-muted hover:text-danger"
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
                      </button>
                    )}
                    {(request.status === "WITHDRAWN" ||
                      request.status === "DISCARDED") && (
                      <span className="text-xs text-faint">已结束</span>
                    )}
                    {request.status === "SOURCING" && (
                      <span className="text-xs text-faint">
                        审批通过，等采购询价（M10）
                      </span>
                    )}
                    {request.status === "REJECTED" && (
                      <span className="text-xs text-faint">已驳回，不能重新提交</span>
                    )}
                  </div>
                </Cell>
              </Row>
            ))}
          </Table>
        )}

        <ErrorNote error={submit.error ?? withdraw.error ?? discard.error} />
      </Card>
    </>
  );
}

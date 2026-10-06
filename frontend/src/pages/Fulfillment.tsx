/** 履约工作台（M10）：询价定标 → 下单收货 → 收票匹配 → 付款归档。
 *
 * ============================ 为什么是"一个工作台 + 队列 + 折叠面板" ============================
 * M10 的后端是 17 条路由、四个子阶段、五种角色。如果把每一步做成一个独立页面，
 * 会有三个问题：
 *
 *   1. **采购专员走完一笔单子要点四五次菜单**，而这是每天重复几十遍的活；
 *   2. 每个页面都要重新查一次那张申请（多几次往返，也多几处不一致的机会）；
 *   3. **谁该看到哪一步** 这件事会散在四五个路由守卫里，早晚有一处漏。
 *
 * 所以这里是一页：上面三个队列（**按角色出现**，没那个角色就不查那个接口），
 * 下面一块折叠面板做完整条链。每一步都显示**当前状态与"下一步该谁做"** ——
 * 这条链的难点从来不是"怎么点"，而是"现在轮到谁"。
 *
 * ============================ 三条不肯让步的约定 ============================
 * 1. **按钮能不能点，由"后端给的状态"决定，不由前端猜**：
 *    `AWARDED` 才显示下单、`INVOICED` 才显示创建付款单……
 *    前端多放一个能点的按钮，用户点下去只会拿到 409。
 * 2. **`version` 一律取自最近一次读到的对象**，不自己加减 ——
 *    这个项目里乐观锁是靠"版本没变"判胜负的。
 * 3. **幂等键在发起动作前生成一把，重试复用**（`lib/idempotency.ts`）。
 */

import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { ApiError } from "../api/client";
import type { QueueItem } from "../api/fulfillment";
import {
  useAddInvoice,
  useAddQuotation,
  useAward,
  useCloseRequest,
  useCompare,
  useCreatePayment,
  useFailPayment,
  useInvoices,
  useMatch,
  useOrder,
  usePay,
  usePaymentQueue,
  usePayments,
  usePlaceOrder,
  useReceiptQueue,
  useReceive,
  useSourcingQueue,
  useVoidInvoice,
} from "../api/hooks/useFulfillment";
import { useMe } from "../api/hooks/useAuth";
import { useSuppliers } from "../api/hooks/useCatalog";
import {
  Badge,
  Button,
  Card,
  Cell,
  EmptyState,
  ErrorNote,
  Field,
  HintNote,
  Input,
  Loading,
  PageHeader,
  RequestStatusBadge,
  Row,
  Select,
  Table,
} from "../components/ui";
import { newIdempotencyKey } from "../lib/idempotency";
import { formatFen, yuanToFen } from "../lib/money";
import { PAYMENT_STATUS_LABEL, QUOTATION_STATUS_LABEL } from "../api/fulfillment";

/** 三个队列分别要的角色。**与后端 `require_roles` 同源**（抄一份就会漂）。 */
const QUEUE_ROLES = {
  sourcing: ["PROCUREMENT"],
  receipt: ["RECEIVER", "PROCUREMENT"],
  payment: ["FINANCE"],
} as const;

function hasAny(roles: string[], wanted: readonly string[]): boolean {
  return roles.some((role) => wanted.includes(role));
}

/** 给"会改变结果"的动作配一把**会随动作轮换**的幂等键。
 *
 * ============================ 两件事必须同时成立 ============================
 * 1. **重试要复用同一把键** —— 网络超时后用户再点一次，服务端才认得出
 *    "这是同一件事"。所以键要在**动作发起前**生成一次，请求过程中保持不变。
 * 2. **失败之后必须换一把** —— 否则下一次点击会带着"上一次失败那条请求的键"
 *    上去，而载荷已经变了（比如用户改了金额），服务端按契约返回 **409「同键不同载荷」**。
 *    那个 409 是对的，但用户看到的是"我明明改了数字，它却说重复提交"。
 *
 * 所以"轮换"的时机是**失败之后**与**成功之后**，而不是每次渲染 ——
 * 每次渲染都新生成一把，等于没有幂等（那是最隐蔽的一种错：测试全绿，
 * 真出网络抖动时才发现重试变成了第二次提交）。
 */
function useIdempotencyKey(): [string, () => void] {
  const [key, setKey] = useState(() => newIdempotencyKey());
  return [key, () => setKey(newIdempotencyKey())];
}

export default function Fulfillment() {
  const me = useMe();
  const [params] = useSearchParams();
  const roles = me.data?.roles ?? [];

  // URL 上带 ?request=<id> 就能直接打开某一张单子（详情页跳过来时用）
  const [picked, setPicked] = useState<QueueItem | null>(null);
  const pickedId = picked?.id ?? params.get("request") ?? null;

  const canSourcing = hasAny(roles, QUEUE_ROLES.sourcing);
  const canReceipt = hasAny(roles, QUEUE_ROLES.receipt);
  const canPayment = hasAny(roles, QUEUE_ROLES.payment);
  const anyQueue = canSourcing || canReceipt || canPayment;

  const sourcing = useSourcingQueue(canSourcing);
  const receipt = useReceiptQueue(canReceipt);
  const payment = usePaymentQueue(canPayment);

  return (
    <>
      <PageHeader
        title="履约工作台"
        description="审批通过之后怎么买：询价定标 → 下单收货 → 收票匹配 → 付款归档。"
        breadcrumb={[{ label: "首页", to: "/" }, { label: "履约工作台" }]}
      />

      {!anyQueue && (
        <Card>
          <h2 className="text-lg font-semibold text-ink">这个账号没有履约环节的角色</h2>
          <p className="mt-2 text-sm text-body">
            履约链按角色分工：<span className="font-mono">PROCUREMENT</span> 负责询价与下单、
            <span className="font-mono"> RECEIVER</span> 负责收货、
            <span className="font-mono"> FINANCE</span> 负责收票与付款。
            没有这些角色时这里没有可做的事 —— **审批与查看申请不受影响**。
          </p>
          <div className="mt-3">
            <HintNote>
              内控要求这三件事由不同的人做（采购不能自己收货），所以角色是分开的。
              明细进度仍然可以在「我的申请」里逐张看到。
            </HintNote>
          </div>
        </Card>
      )}

      {canSourcing && (
        <QueueCard
          title="待询价"
          description="状态 = SOURCING：审批已通过，等着比价定标。"
          query={sourcing}
          pickedId={pickedId}
          onPick={(item) => setPicked(item)}
          emptyText="没有待询价的单子。审批通过的单子会自动出现在这里。"
        />
      )}
      {canReceipt && (
        <QueueCard
          title="待收货"
          description="状态 = ORDERED：已下单，等着验收。"
          query={receipt}
          pickedId={pickedId}
          onPick={(item) => setPicked(item)}
          emptyText="没有待收货的单子。"
        />
      )}
      {canPayment && (
        <QueueCard
          title="待付款"
          description="状态 = INVOICED：已收票，等着付款。⚠️ 能不能付由三单匹配决定。"
          query={payment}
          pickedId={pickedId}
          onPick={(item) => setPicked(item)}
          emptyText="没有待付款的单子。"
        />
      )}

      {pickedId && (
        <RequestFulfillment
          key={pickedId}
          requestId={pickedId}
          roles={roles}
          onClose={() => setPicked(null)}
        />
      )}
    </>
  );
}

// ------------------------------------------------------------------ 队列

function QueueCard({
  title,
  description,
  query,
  pickedId,
  onPick,
  emptyText,
}: {
  title: string;
  description: string;
  query: { data?: { items: QueueItem[]; total: number }; isPending: boolean; isError: boolean; error: unknown };
  pickedId: string | null;
  onPick: (item: QueueItem) => void;
  emptyText: string;
}) {
  return (
    <Card title={title} description={description}>
      {query.isPending && <Loading label="读取队列…" />}
      {query.isError && <ErrorNote error={query.error} />}
      {query.data && query.data.items.length === 0 && (
        <EmptyState>队列是空的 —— {emptyText}</EmptyState>
      )}
      {query.data && query.data.items.length > 0 && (
        <Table head={["单号", "事由", "金额", "状态", ""]}>
          {query.data.items.map((item) => (
            <Row key={item.id} className={item.id === pickedId ? "bg-section/70" : ""}>
              <Cell>
                <span className="font-mono text-xs">{item.code}</span>
              </Cell>
              <Cell>{item.title}</Cell>
              <Cell>{formatFen(item.amount_total_fen)}</Cell>
              <Cell>
                <RequestStatusBadge status={item.status} />
              </Cell>
              <Cell>
                <Button size="sm" variant="ghost" onClick={() => onPick(item)}>
                  打开
                </Button>
              </Cell>
            </Row>
          ))}
        </Table>
      )}
      {query.data && (
        <p className="mt-2 text-xs text-faint">
          共 {query.data.total} 条，按你的可见范围过滤。
        </p>
      )}
    </Card>
  );
}

// ------------------------------------------------------------------ 一张单子的全链

function RequestFulfillment({
  requestId,
  roles,
  onClose,
}: {
  requestId: string;
  roles: string[];
  onClose: () => void;
}) {
  const compare = useCompare(requestId);
  const order = useOrder(requestId);
  const invoices = useInvoices(requestId);
  const match = useMatch(requestId);
  const payments = usePayments(requestId);

  const status = compare.data?.request.status ?? order.data?.order.request_status ?? "";
  const version =
    order.data?.order.request_version ??
    compare.data?.request.version ??
    match.data?.request.version ??
    0;

  return (
    <>
      <Card
        title={`这笔单子的履约进度 · ${compare.data?.request.code ?? ""}`}
        description={compare.data?.request.title}
        action={
          <div className="flex items-center gap-2">
            <RequestStatusBadge status={status} />
            <Button size="sm" variant="ghost" onClick={onClose}>
              收起
            </Button>
          </div>
        }
      >
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <Link className="text-primary hover:underline" to={`/requests/${requestId}`}>
            打开申请详情（含时间线）
          </Link>
          <span className="text-faint">|</span>
          <span className="text-muted">
            版本 {version} —— 每一步都用它做乐观锁
          </span>
        </div>
        <div className="mt-3">
          <ChainProgress status={status} />
        </div>
      </Card>

      {(status === "SOURCING" || compare.data?.quotations.length) && (
        <ComparePanel
          requestId={requestId}
          version={compare.data?.request.version ?? version}
          canAward={hasAny(roles, QUEUE_ROLES.sourcing) && status === "SOURCING"}
        />
      )}

      {(status === "AWARDED" || order.data) && (
        <OrderPanel
          requestId={requestId}
          version={order.data?.order.request_version ?? version}
          canPlace={hasAny(roles, QUEUE_ROLES.sourcing) && status === "AWARDED"}
          canReceive={hasAny(roles, QUEUE_ROLES.receipt) && status === "ORDERED"}
        />
      )}

      {order.data && (
        <InvoicePanel
          requestId={requestId}
          version={version}
          orderSupplierId={order.data.order.supplier_id}
          canWrite={hasAny(roles, ["FINANCE"])}
          status={status}
        />
      )}

      {order.data && match.data && (
        <MatchPanel requestId={requestId} version={version} canPay={hasAny(roles, ["FINANCE"])} />
      )}

      {match.data && (status === "INVOICED" || payments.data?.length) && (
        <PaymentPanel
          requestId={requestId}
          version={version}
          canWrite={hasAny(roles, ["FINANCE"])}
          status={status}
        />
      )}

      {invoices.isError && <ErrorNote error={invoices.error} />}
    </>
  );
}

/** 链路进度条：把"现在在哪一步"画出来。
 *
 * 这条链有九段，用户最容易迷路的正是"现在轮到谁"。所以每一步都标出
 * **已完成 / 当前 / 未开始**，而不是只显示一个状态字。
 */
function ChainProgress({ status }: { status: string }) {
  const steps = [
    { key: "APPROVED", label: "审批通过", at: ["SOURCING"] },
    { key: "AWARDED", label: "已定标", at: ["AWARDED"] },
    { key: "ORDERED", label: "已下单", at: ["ORDERED"] },
    { key: "RECEIVED", label: "已收货", at: ["RECEIVED"] },
    { key: "INVOICED", label: "已收票", at: ["INVOICED"] },
    { key: "PAID", label: "已付款", at: ["PAID"] },
    { key: "CLOSED", label: "已归档", at: ["CLOSED"] },
  ];
  const currentIndex = steps.findIndex((step) => step.at.includes(status));

  return (
    <ol className="flex flex-wrap items-center gap-x-1 gap-y-2 text-xs">
      {steps.map((step, index) => {
        const done = currentIndex >= 0 && index < currentIndex;
        const current = index === currentIndex;
        return (
          <li key={step.key} className="flex items-center gap-1">
            {index > 0 && <span className="text-faint">→</span>}
            <span
              className={
                "rounded-sm px-2 py-0.5 " +
                (current
                  ? "bg-primary text-primary-fg"
                  : done
                    ? "bg-success-bg text-success"
                    : "bg-section text-faint")
              }
            >
              {step.label}
            </span>
          </li>
        );
      })}
      {currentIndex < 0 && (
        <li className="text-muted">
          （当前状态 {status} 不在这条链上）
        </li>
      )}
    </ol>
  );
}

// ------------------------------------------------------------------ 比价与定标

function ComparePanel({
  requestId,
  version,
  canAward,
}: {
  requestId: string;
  version: number;
  canAward: boolean;
}) {
  const compare = useCompare(requestId);
  const suppliers = useSuppliers();
  const add = useAddQuotation(requestId);
  const award = useAward(requestId);

  const [supplierId, setSupplierId] = useState("");
  const [amountYuan, setAmountYuan] = useState("");
  const [validUntil, setValidUntil] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);

  // 定标的幂等键：**点下去之前生成一把**，重试复用；失败后轮换（见 useIdempotencyKey）
  const [awardKey, rotateAwardKey] = useIdempotencyKey();

  function submitQuotation() {
    setLocalError(null);
    let fen: number;
    try {
      fen = yuanToFen(amountYuan);
    } catch (error) {
      setLocalError((error as Error).message);
      return;
    }
    if (!supplierId) {
      setLocalError("先选一家供应商");
      return;
    }
    add.mutate(
      {
        supplier_id: supplierId,
        amount_fen: fen,
        valid_until: validUntil ? new Date(validUntil).toISOString() : null,
      },
      {
        onSuccess: () => {
          setAmountYuan("");
          setValidUntil("");
        },
      },
    );
  }

  return (
    <Card
      title="比价与定标"
      description={`金额 ≥ ${formatFen(compare.data?.threshold_fen ?? 0)} 时需要至少 ${compare.data?.min_quotations ?? 3} 家有效报价。`}
    >
      {compare.isPending && <Loading label="读取比价表…" />}
      {compare.isError && <ErrorNote error={compare.error} />}

      {compare.data && (
        <>
          {compare.data.quotations.length === 0 ? (
            <EmptyState>还没有报价 —— 登记至少一家供应商的报价，然后才能定标。</EmptyState>
          ) : (
            <Table head={["供应商", "报价", "状态", "有效期", "操作"]}>
              {compare.data.quotations.map((quotation) => (
                <Row key={quotation.id}>
                  <Cell>{quotation.supplier_name}</Cell>
                  <Cell>{formatFen(quotation.amount_fen)}</Cell>
                  <Cell>
                    <Badge
                      tone={
                        quotation.status === "SELECTED"
                          ? "success"
                          : quotation.status === "SUBMITTED"
                            ? "warning"
                            : "neutral"
                      }
                    >
                      {QUOTATION_STATUS_LABEL[quotation.status]}
                    </Badge>
                  </Cell>
                  <Cell>
                    {quotation.valid_until
                      ? new Date(quotation.valid_until).toLocaleDateString()
                      : "长期有效"}
                  </Cell>
                  <Cell>
                    {canAward && quotation.status === "SUBMITTED" && (
                      <Button
                        size="sm"
                        disabled={award.isPending}
                        onClick={() =>
                          award.mutate(
                            {
                              supplier_id: quotation.supplier_id,
                              version,
                              idempotencyKey: awardKey,
                            },
                            // 失败之后换键：下一次点击是新的一次动作，
                            // 不能带着上一次失败那条请求的键（那会撞 409「同键不同载荷」）
                            { onError: rotateAwardKey },
                          )
                        }
                      >
                        定标给它
                      </Button>
                    )}
                  </Cell>
                </Row>
              ))}
            </Table>
          )}

          {/* `problems` 是**当前还差什么**，不是错误 —— 所以用提示条而不是红框 */}
          {compare.data.problems.length > 0 && (
            <div className="mt-3">
              <HintNote>
                还不能定标：{compare.data.problems.join("；")}
              </HintNote>
            </div>
          )}

          {add.isError && (
            <div className="mt-3">
              <ErrorNote error={add.error} />
            </div>
          )}
          {award.isError && (
            <div className="mt-3">
              <ErrorNote error={award.error} />
            </div>
          )}

          {canAward && (
            <div className="mt-4 grid gap-3 border-t border-line pt-4 sm:grid-cols-4">
              <Field label="供应商">
                <Select
                  value={supplierId}
                  onChange={(event) => setSupplierId(event.target.value)}
                >
                  <option value="">请选择</option>
                  {(suppliers.data ?? []).map((supplier) => (
                    <option key={supplier.id} value={supplier.id}>
                      {supplier.name}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label="报价金额（元）" hint="整数分入库，这里填元">
                <Input
                  data-testid="quotation-amount"
                  value={amountYuan}
                  onChange={(event) => setAmountYuan(event.target.value)}
                  placeholder="例如 1200.00"
                />
              </Field>
              <Field label="报价有效期" hint="留空 = 长期有效">
                <Input
                  data-testid="quotation-valid-until"
                  type="date"
                  value={validUntil}
                  onChange={(event) => setValidUntil(event.target.value)}
                />
              </Field>
              <div className="flex items-end">
                <Button
                  onClick={submitQuotation}
                  disabled={add.isPending}
                  className="w-full"
                >
                  {add.isPending ? "登记中…" : "登记报价"}
                </Button>
              </div>
              {localError && (
                <p className="text-sm text-danger sm:col-span-4">{localError}</p>
              )}
            </div>
          )}
        </>
      )}
    </Card>
  );
}

// ------------------------------------------------------------------ 订单与收货

function OrderPanel({
  requestId,
  version,
  canPlace,
  canReceive,
}: {
  requestId: string;
  version: number;
  canPlace: boolean;
  canReceive: boolean;
}) {
  const order = useOrder(requestId);
  const place = usePlaceOrder(requestId);
  const receive = useReceive(requestId);
  const [placeKey, rotatePlaceKey] = useIdempotencyKey();
  const [receiveKey, rotateReceiveKey] = useIdempotencyKey();
  const [accepted, setAccepted] = useState<Record<string, number>>({});
  const [note, setNote] = useState("");

  const lines = order.data?.lines ?? [];
  const receipt = order.data?.receipt ?? null;

  return (
    <Card
      title="采购订单"
      description="订单明细是申请明细的**快照**，单价取定标成交单价 —— 所以订单金额通常小于申请金额。"
    >
      {order.isPending && <Loading label="读取订单…" />}
      {order.isError && <ErrorNote error={order.error} />}

      {order.data === null && (
        <>
          <EmptyState>还没下单 —— 定标之后才能生成订单（金额由后端按明细重算，不接受客户端传值）。</EmptyState>
          {canPlace && (
            <div className="mt-3">
              <Button
                disabled={place.isPending}
                onClick={() =>
                  place.mutate(
                    { version, idempotencyKey: placeKey },
                    { onError: rotatePlaceKey },
                  )
                }
              >
                {place.isPending ? "生成中…" : "由定标结果生成订单"}
              </Button>
            </div>
          )}
        </>
      )}

      {order.data && (
        <>
          <div className="mb-3 flex flex-wrap items-center gap-3 text-sm">
            <span className="font-mono text-xs">{order.data.order.order_no}</span>
            <Badge tone="success">{formatFen(order.data.order.amount_fen)}</Badge>
            <span className="text-muted">
              订单金额 = Σ(成交单价 × 数量)，与申请金额不同是正常的
            </span>
          </div>

          <Table head={["#", "名称", "数量", "单价", "金额"]}>
            {lines.map((line) => (
              <Row key={line.id}>
                <Cell>{line.line_no}</Cell>
                <Cell>
                  {line.name}
                  {line.spec && <span className="ml-1 text-xs text-faint">{line.spec}</span>}
                </Cell>
                <Cell>{line.quantity}</Cell>
                <Cell>{formatFen(line.unit_price_fen)}</Cell>
                <Cell>{formatFen(line.amount_fen)}</Cell>
              </Row>
            ))}
          </Table>

          {receipt ? (
            <div className="mt-3">
              <HintNote>
                已收货（{new Date(receipt.received_at).toLocaleString()}）
                {receipt.note ? ` · 备注：${receipt.note}` : ""}
                {" · "}
                {receipt.fully_received ? "整单收齐" : "未收齐"}
              </HintNote>
            </div>
          ) : (
            canReceive && (
              <div className="mt-4 border-t border-line pt-4">
                <p className="mb-3 text-sm text-body">
                  <strong className="text-ink">整单收货</strong>：逐行填实收数量，
                  <strong>每一行都要收齐</strong>才能登记（第一版不支持部分收货 ——
                  收一半会被拒绝并列出差哪几行）。
                </p>
                <Table head={["名称", "订单数量", "实收数量"]}>
                  {lines.map((line) => (
                    <Row key={line.id}>
                      <Cell>{line.name}</Cell>
                      <Cell>{line.quantity}</Cell>
                      <Cell>
                        <Input
                          type="number"
                          min={0}
                          max={line.quantity}
                          className="w-24"
                          value={accepted[line.id] ?? line.quantity}
                          onChange={(event) =>
                            setAccepted((prev) => ({
                              ...prev,
                              [line.id]: Number(event.target.value),
                            }))
                          }
                        />
                      </Cell>
                    </Row>
                  ))}
                </Table>
                <div className="mt-3 grid gap-3 sm:grid-cols-3">
                  <Field label="备注" className="sm:col-span-2">
                    <Input
                      value={note}
                      onChange={(event) => setNote(event.target.value)}
                      placeholder="选填，例如「外箱轻微破损，内部完好」"
                    />
                  </Field>
                  <div className="flex items-end">
                    <Button
                      className="w-full"
                      disabled={receive.isPending}
                      onClick={() =>
                        receive.mutate(
                          {
                            accepted: Object.fromEntries(
                              lines.map((line) => [
                                line.id,
                                accepted[line.id] ?? line.quantity,
                              ]),
                            ),
                            note: note || null,
                            version,
                            idempotencyKey: receiveKey,
                          },
                          // 失败（例如"没齐，差订书机 1 件"）之后换键：
                          // 用户改完数量再提交是新的一次动作，不该复用旧键
                          { onError: rotateReceiveKey },
                        )
                      }
                    >
                      {receive.isPending ? "登记中…" : "登记收货"}
                    </Button>
                  </div>
                </div>
              </div>
            )
          )}

          {place.isError && (
            <div className="mt-3">
              <ErrorNote error={place.error} />
            </div>
          )}
          {receive.isError && (
            <div className="mt-3">
              <ErrorNote error={receive.error} />
            </div>
          )}
        </>
      )}
    </Card>
  );
}

// ------------------------------------------------------------------ 发票

function InvoicePanel({
  requestId,
  version,
  orderSupplierId,
  canWrite,
  status,
}: {
  requestId: string;
  version: number;
  orderSupplierId: string;
  canWrite: boolean;
  status: string;
}) {
  const invoices = useInvoices(requestId);
  const add = useAddInvoice(requestId);
  const voidInvoice = useVoidInvoice(requestId);
  const [invoiceNo, setInvoiceNo] = useState("");
  const [amountYuan, setAmountYuan] = useState("");
  const [taxYuan, setTaxYuan] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);
  const [addKey, rotateAddKey] = useIdempotencyKey();

  function submit() {
    setLocalError(null);
    let amount: number;
    let tax: number;
    try {
      amount = yuanToFen(amountYuan);
      tax = taxYuan === "" ? 0 : yuanToFen(taxYuan);
    } catch (error) {
      setLocalError((error as Error).message);
      return;
    }
    if (!invoiceNo.trim()) {
      setLocalError("发票号不能为空");
      return;
    }
    add.mutate(
      {
        supplier_id: orderSupplierId,
        invoice_no: invoiceNo.trim(),
        amount_fen: amount,
        tax_amount_fen: tax,
        version,
        idempotencyKey: addKey,
      },
      {
        onSuccess: () => {
          setInvoiceNo("");
          setAmountYuan("");
          setTaxYuan("");
          // 下一次登记是一张**新票**，所以要换一把新键 ——
          // 复用同一把会被服务端判成「同键不同载荷」（409 改了主意）。
          rotateAddKey();
        },
      },
    );
  }

  const canWriteNow = canWrite && ["RECEIVED", "INVOICED"].includes(status);

  return (
    <Card
      title="发票"
      description="可以登记多张（分批开票），三单匹配用的是**有效发票之和**；作废的票不参与求和。"
    >
      {invoices.isPending && <Loading label="读取发票…" />}
      {invoices.isError && <ErrorNote error={invoices.error} />}

      {invoices.data && invoices.data.length === 0 && (
        <EmptyState>还没登记发票 —— 收货之后才能登记发票。</EmptyState>
      )}

      {invoices.data && invoices.data.length > 0 && (
        <Table head={["发票号", "金额", "税额", "开票日期", "状态", "操作"]}>
          {invoices.data.map((invoice) => (
            <Row key={invoice.id}>
              <Cell>
                <span className="font-mono text-xs">{invoice.invoice_no}</span>
              </Cell>
              <Cell>{formatFen(invoice.amount_fen)}</Cell>
              <Cell>{formatFen(invoice.tax_amount_fen)}</Cell>
              <Cell>{invoice.issued_on ?? "—"}</Cell>
              <Cell>
                {/* 作废票必须**显式标出来**：它在列表里，但不参与求和 ——
                    不标的话用户会以为那张票还能拿去付款。 */}
                <Badge tone={invoice.status === "ACTIVE" ? "success" : "neutral"}>
                  {invoice.status === "ACTIVE" ? "有效" : "已作废"}
                </Badge>
              </Cell>
              <Cell>
                {canWriteNow && invoice.status === "ACTIVE" && (
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={voidInvoice.isPending}
                    onClick={() => {
                      const reason = window.prompt("作废原因（会写进时间线）：", "重复开票");
                      if (reason === null) return;
                      voidInvoice.mutate({ invoiceId: invoice.id, reason });
                    }}
                  >
                    作废
                  </Button>
                )}
              </Cell>
            </Row>
          ))}
        </Table>
      )}

      {voidInvoice.isError && (
        <div className="mt-3">
          <ErrorNote error={voidInvoice.error} />
        </div>
      )}
      {add.isError && (
        <div className="mt-3">
          <ErrorNote error={add.error} />
        </div>
      )}

      {canWriteNow ? (
        <div className="mt-4 grid gap-3 border-t border-line pt-4 sm:grid-cols-4">
          <Field label="发票号">
            <Input
              data-testid="invoice-no"
              value={invoiceNo}
              onChange={(event) => setInvoiceNo(event.target.value)}
              placeholder="例如 04412"
            />
          </Field>
          <Field label="金额（元）" hint="含税总额，匹配比的就是它">
            <Input
              data-testid="invoice-amount"
              value={amountYuan}
              onChange={(event) => setAmountYuan(event.target.value)}
            />
          </Field>
          <Field label="税额（元）" hint="只记录，不参与匹配">
            <Input
              data-testid="invoice-tax"
              value={taxYuan}
              onChange={(event) => setTaxYuan(event.target.value)}
            />
          </Field>
          <div className="flex items-end">
            <Button className="w-full" disabled={add.isPending} onClick={submit}>
              {add.isPending ? "登记中…" : "登记发票"}
            </Button>
          </div>
          {localError && (
            <p className="text-sm text-danger sm:col-span-4">{localError}</p>
          )}
        </div>
      ) : (
        canWrite && (
          <div className="mt-3">
            <HintNote>
              当前状态 {status} 不能登记发票 —— 需要先收货（没收货就收票等于跳过验收）。
            </HintNote>
          </div>
        )
      )}
    </Card>
  );
}

// ------------------------------------------------------------------ 三单匹配

function MatchPanel({
  requestId,
  version,
  canPay,
}: {
  requestId: string;
  version: number;
  canPay: boolean;
}) {
  const match = useMatch(requestId);

  return (
    <Card
      title="三单匹配"
      description="订单 / 收货 / 发票三列对比。这里**只读**，真正的拦截点在「执行付款」那一刻。"
    >
      {match.isPending && <Loading label="跑匹配…" />}
      {match.isError && <ErrorNote error={match.error} />}

      {match.data && (
        <>
          <Table head={["匹配项", "采购订单", "收货单", "发票", "结论"]}>
            <Row>
              <Cell>数量</Cell>
              <Cell>{match.data.order ? match.data.order.ordered_qty : "—"}</Cell>
              <Cell>{match.data.receipt.receipt_exists ? match.data.receipt.accepted_qty : "—"}</Cell>
              <Cell>—</Cell>
              <Cell>
                <IssueCell
                  issues={match.data.issues}
                  code="OVER_RECEIPT"
                  okText="实收 ≤ 订单"
                />
              </Cell>
            </Row>
            <Row>
              <Cell>金额</Cell>
              <Cell>{match.data.order ? formatFen(match.data.order.amount_fen) : "—"}</Cell>
              <Cell>—</Cell>
              <Cell>{formatFen(match.data.invoice_total_fen)}</Cell>
              <Cell>
                <IssueCell
                  issues={match.data.issues}
                  code="AMOUNT_MISMATCH"
                  okText="在容差内"
                />
              </Cell>
            </Row>
            <Row>
              <Cell>供应商</Cell>
              <Cell>
                <span className="font-mono text-xs">
                  {match.data.order ? match.data.order.supplier_id.slice(0, 8) : "—"}
                </span>
              </Cell>
              <Cell>—</Cell>
              <Cell>
                <span className="font-mono text-xs">
                  {match.data.all_suppliers_match ? "全部一致" : "有不一致"}
                </span>
              </Cell>
              <Cell>
                <IssueCell
                  issues={match.data.issues}
                  code="SUPPLIER_MISMATCH"
                  okText="一致"
                />
              </Cell>
            </Row>
            <Row>
              <Cell>张数</Cell>
              <Cell>{match.data.order ? `${match.data.order.line_count} 行` : "—"}</Cell>
              <Cell>—</Cell>
              <Cell>{match.data.invoice_count} 张有效</Cell>
              <Cell>
                {match.data.invoice_count === 0 && (
                  <Badge tone="danger">{ISSUE_FALLBACK.NO_INVOICE}</Badge>
                )}
              </Cell>
            </Row>
          </Table>

          {match.data.passed ? (
            <div className="mt-3">
              <HintNote>
                ✅ 三单匹配通过 —— 可以执行付款（付款那一刻会**再算一次**，
                因为发票可能在那之后被改）。
              </HintNote>
            </div>
          ) : (
            <div className="mt-3 space-y-2">
              {match.data.issues.map((issue) => (
                <div
                  key={issue.code + String(issue.expected) + String(issue.actual)}
                  className="rounded-md border border-danger/40 bg-danger-bg px-3 py-2 text-sm text-danger"
                >
                  <span className="font-mono text-xs">{issue.code}</span> {issue.message}
                  {issue.expected !== null && issue.actual !== null && (
                    <span className="ml-1">
                      （应为 {formatFen(issue.expected)}，实际 {formatFen(issue.actual)}）
                    </span>
                  )}
                </div>
              ))}
            </div>
          )}

          <p className="mt-3 text-xs text-faint">
            申请金额 {formatFen(match.data.request.request_amount_fen)} —— **它不是匹配基准**。
            比的是订单金额，两者不同是正常的（询价压价，差额由预算显式释放）。
          </p>

          {!canPay && (
            <div className="mt-3">
              <HintNote>
                执行付款需要 <span className="font-mono">FINANCE</span> 角色。
                这一步会真的动预算（写 `SPEND` 与 `RELEASE` 两笔流水）。
              </HintNote>
            </div>
          )}
          <input type="hidden" value={version} readOnly />
        </>
      )}
    </Card>
  );
}

/** 「没有订单 / 没有发票」这两个码没有对应的一列，单独给文案。 */
const ISSUE_FALLBACK = {
  NO_INVOICE: "没有有效发票",
  NO_ORDER: "还没有订单",
} as const;

function IssueCell({
  issues,
  code,
  okText,
}: {
  issues: { code: string }[];
  code: string;
  okText: string;
}) {
  const hit = issues.some((issue) => issue.code === code);
  return hit ? <Badge tone="danger">不通过</Badge> : <Badge tone="success">{okText}</Badge>;
}

// ------------------------------------------------------------------ 付款与归档

function PaymentPanel({
  requestId,
  version,
  canWrite,
  status,
}: {
  requestId: string;
  version: number;
  canWrite: boolean;
  status: string;
}) {
  const payments = usePayments(requestId);
  const create = useCreatePayment(requestId);
  const pay = usePay(requestId);
  const fail = useFailPayment(requestId);
  const close = useCloseRequest(requestId);
  const [createKey, rotateCreateKey] = useIdempotencyKey();
  const [payKey, rotatePayKey] = useIdempotencyKey();

  const active = payments.data?.find((payment) => payment.status !== "FAILED") ?? null;
  const lastPaid = payments.data?.find((payment) => payment.status === "PAID") ?? null;

  return (
    <Card
      title="付款与归档"
      description="付款是这个系统里唯一真正动钱的操作：先抢付款权，再动预算（SPEND 实付 + RELEASE 差额）。"
    >
      {payments.isPending && <Loading label="读取付款单…" />}
      {payments.isError && <ErrorNote error={payments.error} />}

      {payments.data && (
        <>
          {payments.data.length === 0 ? (
            <EmptyState>还没创建付款单 —— 创建付款单只校验「已收票 + 有有效发票」；三单匹配在付款那一刻才跑。</EmptyState>
          ) : (
            <Table head={["第几次", "金额", "状态", "创建时间", "付款时间", "失败原因"]}>
              {payments.data.map((payment) => (
                <Row key={payment.id}>
                  <Cell>第 {payment.attempt_no} 次</Cell>
                  <Cell>{formatFen(payment.amount_fen)}</Cell>
                  <Cell>
                    <Badge
                      tone={
                        payment.status === "PAID"
                          ? "success"
                          : payment.status === "FAILED"
                            ? "danger"
                            : "warning"
                      }
                    >
                      {PAYMENT_STATUS_LABEL[payment.status]}
                    </Badge>
                  </Cell>
                  <Cell>{new Date(payment.created_at).toLocaleString()}</Cell>
                  <Cell>
                    {payment.paid_at ? new Date(payment.paid_at).toLocaleString() : "—"}
                  </Cell>
                  <Cell>{payment.failure_reason ?? "—"}</Cell>
                </Row>
              ))}
            </Table>
          )}

          <div className="mt-4 flex flex-wrap gap-2 border-t border-line pt-4">
            {canWrite && status === "INVOICED" && !active && (
              <Button
                disabled={create.isPending}
                onClick={() => create.mutate(createKey, { onError: rotateCreateKey })}
              >
                {create.isPending ? "创建中…" : "创建付款单"}
              </Button>
            )}

            {canWrite && status === "INVOICED" && active && active.status === "PENDING" && (
              <>
                <Button
                  disabled={pay.isPending}
                  onClick={() =>
                    pay.mutate(
                      {
                        paymentId: active.id,
                        version,
                        idempotencyKey: payKey,
                      },
                      // 付款失败（例如匹配没过）之后换键：用户改完发票再付是新的一次动作
                      { onError: rotatePayKey },
                    )
                  }
                >
                  {pay.isPending ? "付款中…" : `执行付款（${formatFen(active.amount_fen)}）`}
                </Button>
                <Button
                  variant="ghost"
                  disabled={fail.isPending}
                  onClick={() => {
                    const reason = window.prompt(
                      "标记失败的原因（例如「银行退回：账号不符」）：",
                      "银行退回",
                    );
                    if (!reason) return;
                    fail.mutate({ paymentId: active.id, reason });
                  }}
                >
                  标记付款失败
                </Button>
              </>
            )}

            {canWrite && status === "PAID" && (
              <Button
                variant="ghost"
                disabled={close.isPending}
                onClick={() => close.mutate({ version })}
              >
                {close.isPending ? "归档中…" : "归档（终态）"}
              </Button>
            )}

            {!canWrite && (
              <span className="text-xs text-muted">
                付款与归档需要 <span className="font-mono">FINANCE</span> 角色。
              </span>
            )}
          </div>

          {pay.isSuccess && (
            <div className="mt-3">
              <HintNote>
                付款完成：实付 {formatFen(pay.data.amount_fen)}，
                释放预留差额 {formatFen(pay.data.released_fen)}。
                两笔流水都已写入（`SPEND` 与 `RELEASE`）。
              </HintNote>
            </div>
          )}
          {lastPaid && !pay.isSuccess && (
            <div className="mt-3">
              <HintNote>
                已于 {new Date(lastPaid.paid_at ?? lastPaid.created_at).toLocaleString()} 付款
                {formatFen(lastPaid.amount_fen)}。
              </HintNote>
            </div>
          )}

          {create.isError && (
            <div className="mt-3">
              <ErrorNote error={create.error} />
            </div>
          )}
          {pay.isError && (
            <div className="mt-3">
              <ErrorNote error={pay.error} />
            </div>
          )}
          {fail.isError && (
            <div className="mt-3">
              <ErrorNote error={fail.error} />
            </div>
          )}
          {close.isError && (
            <div className="mt-3">
              <ErrorNote error={close.error} />
            </div>
          )}
        </>
      )}
    </Card>
  );
}

/** 把 ApiError 的 message 提出来给 window.prompt 之外的地方用（保留给将来）。 */
export function messageOf(error: unknown): string {
  return error instanceof ApiError ? error.message : String(error);
}

/** 履约链（M10）的接口：询价与定标、订单与收货、发票与三单匹配、付款与归档。
 *
 * ============================ 为什么单独一个文件 ============================
 * `client.ts` 已经 840 多行。M10 一次加了 17 条路由，再堆进去会让那个文件
 * 变成"什么都往里塞"的地方 —— 而它本来就承担了鉴权续期、上传进度、
 * 统一错误体这些**横向**职责。分部委（履约链 / 后续的 AI）读起来清楚得多。
 *
 * ============================ 三条与前面对齐的纪律 ============================
 * 1. **金额一律「分」**：字段名带 `_fen`，界面上要显示「元」时只经 `lib/money.ts`。
 * 2. **会重复执行出不同结果的写操作带幂等键**：定标、下单、收货、登记发票、
 *    创建付款单、执行付款 —— 它们的 `Idempotency-Key` 由调用方生成
 *    （见 `lib/idempotency.ts`），并且**同一个动作要复用同一把键**，
 *    否则"重试"会变成"又做一次"。
 * 3. **乐观锁的 `version` 必须回传**：本层不替调用方猜版本 ——
 *    所有写接口的 `version` 都由调用方从最近一次读到的对象里取。
 */

import { request } from "./client";

// ================================================================ 类型

/** 履约链里的申请摘要（队列用；字段比 `PurchaseRequest` 少，够排队看就行）。 */
export type QueueItem = {
  id: string;
  code: string;
  title: string;
  amount_total_fen: number;
  status: string;
  version: number;
};

export type QueuePage = { items: QueueItem[]; total: number };

// ------------------------------------------------ 询价与定标（8.1）

export type QuotationStatus = "SUBMITTED" | "EXPIRED" | "SELECTED" | "NOT_SELECTED";

export type Quotation = {
  id: string;
  request_id: string;
  supplier_id: string;
  supplier_name: string;
  amount_fen: number;
  currency: string;
  status: QuotationStatus;
  valid_until: string | null;
  attachment_id: string | null;
  created_by: string;
  created_at: string;
};

/** 比价表。
 *
 * ⚠️ `problems` **不是错误**：它是"当前还差什么才能定标"的说明
 * （例如"至少需要 3 家…还差 1 家"）。读比价表永远成功，
 * 真正会拒绝的是定标 —— 所以界面上要把 `problems` 当**提示**渲染，不是报错条。
 */
export type Compare = {
  request: QueueItem;
  quotations: Quotation[];
  problems: string[];
  threshold_fen: number;
  min_quotations: number;
};

export type AwardResult = {
  request: QueueItem;
  quotation: Quotation;
  deal_unit_price_fen: number;
};

// ------------------------------------------------ 订单与收货（8.2）

export type OrderLine = {
  id: string;
  order_id: string;
  item_id: string | null;
  line_no: number;
  name: string;
  spec: string | null;
  quantity: number;
  unit_price_fen: number;
  amount_fen: number;
};

/** 订单。
 *
 * `amount_fen` 是**订单金额**（= Σ 明细行金额），它**通常小于申请金额**（询价压价），
 * 差额由预算的"实付小于预留则显式释放"处理 —— 这是口径，不是 bug。
 * `request_status` / `request_version` 是**申请**的状态与版本（写操作要用后者）。
 */
export type Order = {
  id: string;
  request_id: string;
  order_no: string;
  supplier_id: string;
  amount_fen: number;
  status: string;
  ordered_at: string;
  request_status: string;
  request_version: number;
};

/** 收货单。
 *
 * `fully_received` 是"这张申请收齐了没有"的判定，由后端算 ——
 * 客户端**不要**自己按状态推（那会随流程倒退，后端修过一次）。
 */
export type Receipt = {
  id: string;
  request_id: string;
  order_id: string;
  receiver_id: string;
  status: string;
  note: string | null;
  received_at: string;
  fully_received: boolean;
};

/** 订单详情。`receipt` 为 `null` 表示**还没收货**（不是错误）。 */
export type OrderDetail = {
  order: Order;
  lines: OrderLine[];
  receipt: Receipt | null;
};

export type PlaceOrderResult = { order: Order; lines: OrderLine[] };
export type ReceiveResult = { order: Order; receipt: Receipt };

// ------------------------------------------------ 发票与三单匹配（8.3）

export type InvoiceStatus = "ACTIVE" | "VOIDED";

/** 发票。
 *
 * `amount_fen` 是**含税总额** —— 三单匹配比的就是它；
 * `tax_amount_fen` 只记录、不参与匹配。
 * `status` 为 `VOIDED` 的票**不参与金额求和**，界面上必须标出来
 * （否则用户会以为那张票还能拿去付款）。
 */
export type Invoice = {
  id: string;
  request_id: string;
  supplier_id: string;
  invoice_no: string;
  status: InvoiceStatus;
  amount_fen: number;
  tax_amount_fen: number;
  issued_on: string | null;
  attachment_id: string | null;
  created_at: string;
};

export type InvoiceAdded = {
  invoice: Invoice;
  invoice_total_fen: number;
  invoice_count: number;
  request_status: string;
  request_version: number;
};

export type MatchIssueCode =
  | "OVER_RECEIPT"
  | "AMOUNT_MISMATCH"
  | "SUPPLIER_MISMATCH"
  | "NO_ORDER"
  | "NO_INVOICE";

/** 一条不匹配项。界面按 `code` 决定高亮哪一列，按 `expected`/`actual` 显示差异。 */
export type MatchIssue = {
  code: MatchIssueCode;
  message: string;
  expected: number | null;
  actual: number | null;
};

export type MatchOrder = {
  id: string;
  order_no: string;
  supplier_id: string;
  amount_fen: number;
  ordered_qty: number;
  line_count: number;
};

export type MatchReceipt = {
  accepted_qty: number;
  receipt_exists: boolean;
  receipt_id: string | null;
};

/** 三单匹配预检结果（**只读**，不改任何状态）。
 *
 * `passed` 只是个便捷布尔值；**判定要以 `issues` 为准** ——
 * 问题列表能说清"哪里不对、差多少"，那才是它存在的意义。
 */
export type MatchResult = {
  request: {
    id: string;
    code: string;
    title: string;
    status: string;
    version: number;
    /** 申请金额。**不是匹配基准** —— 显示它是为了让人看到"申请 vs 订单"的差额 */
    request_amount_fen: number;
  };
  order: MatchOrder | null;
  receipt: MatchReceipt;
  invoices: Invoice[];
  invoice_total_fen: number;
  invoice_count: number;
  all_suppliers_match: boolean;
  passed: boolean;
  issues: MatchIssue[];
};

// ------------------------------------------------ 付款与归档（8.4）

export type PaymentStatus = "PENDING" | "PAID" | "FAILED";

/** 付款单。
 *
 * `amount_fen` 是**实付**（= 有效发票之和）。
 * `attempt_no`：同一张申请第几次尝试 —— `FAILED` 的行会留在列表里，
 * 界面要能说"这是第 2 次尝试"，否则多行会让人以为系统重复建单了。
 */
export type Payment = {
  id: string;
  request_id: string;
  amount_fen: number;
  status: PaymentStatus;
  attempt_no: number;
  failure_reason: string | null;
  created_by: string;
  created_at: string;
  paid_at: string | null;
};

export type PaymentCreated = {
  payment: Payment;
  invoice_total_fen: number;
  invoice_count: number;
  request_status: string;
  request_version: number;
};

/** 执行付款的结果。
 *
 * `amount_fen` 与 `released_fen` 是**两笔流水的金额**，显式回给界面 ——
 * 财务点完"付款"要立刻看到"付了多少、释放了多少"，而不是自己去算。
 */
export type PayResult = {
  payment: Payment;
  request_status: string;
  request_version: number;
  amount_fen: number;
  released_fen: number;
};

export type CloseResult = { request_id: string; status: string; version: number };

// ================================================================ 接口

export const fulfillmentApi = {
  // ------------------------------------------------ 询价与定标（8.1）

  /** 待询价队列（`SOURCING`）。`PROCUREMENT` 角色。 */
  sourcingQueue: (params: { limit?: number; offset?: number } = {}) => {
    const query = new URLSearchParams();
    if (params.limit !== undefined) query.set("limit", String(params.limit));
    if (params.offset !== undefined) query.set("offset", String(params.offset));
    const suffix = query.toString() ? `?${query}` : "";
    return request<QueuePage>(`/api/v1/sourcing/queue${suffix}`);
  },

  /** 比价表：各家报价 + "还差什么才能定标"。**读永远成功**。 */
  compare: (requestId: string) =>
    request<Compare>(`/api/v1/requests/${requestId}/quotations`),

  /** 登记一家报价。同一家在同一张申请上只能报一次（重复 → 409）。 */
  addQuotation: (
    requestId: string,
    body: {
      supplier_id: string;
      amount_fen: number;
      valid_until?: string | null;
      attachment_id?: string | null;
    },
  ) =>
    request<Quotation>(`/api/v1/requests/${requestId}/quotations`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  /** 定标。**必须带幂等键**：它改变了主单状态，超时重试要拿回第一次的结果。 */
  award: (
    requestId: string,
    body: { supplier_id: string; version: number },
    idempotencyKey: string,
  ) =>
    request<AwardResult>(`/api/v1/requests/${requestId}/award`, {
      method: "POST",
      body: JSON.stringify(body),
      headers: { "Idempotency-Key": idempotencyKey },
    }),

  // ------------------------------------------------ 订单与收货（8.2）

  /** 待收货队列（`ORDERED`）。`RECEIVER` / `PROCUREMENT`。 */
  receiptQueue: (params: { limit?: number; offset?: number } = {}) => {
    const query = new URLSearchParams();
    if (params.limit !== undefined) query.set("limit", String(params.limit));
    if (params.offset !== undefined) query.set("offset", String(params.offset));
    const suffix = query.toString() ? `?${query}` : "";
    return request<QueuePage>(`/api/v1/receipts/queue${suffix}`);
  },

  /** 下单（由定标结果生成订单）。金额、供应商、明细都不接受客户端传值。 */
  placeOrder: (requestId: string, version: number, idempotencyKey: string) =>
    request<PlaceOrderResult>(`/api/v1/requests/${requestId}/orders`, {
      method: "POST",
      body: JSON.stringify({ version }),
      headers: { "Idempotency-Key": idempotencyKey },
    }),

  /** 按申请查订单。**没下过单返回 `null`**（不是 404）——
   *  详情页要能一屏显示"申请 + 订单（若有）+ 收货（若有）"。 */
  orderOfRequest: (requestId: string) =>
    request<OrderDetail | null>(`/api/v1/requests/${requestId}/orders`),

  /** 订单详情（订单 + 明细 + 收货单）。 */
  getOrder: (orderId: string) => request<OrderDetail>(`/api/v1/orders/${orderId}`),

  /** 收货登记（**整单收货**：每一行都要填，没收齐会被 422 挡回并列出差哪几行）。 */
  receive: (
    requestId: string,
    body: { accepted: Record<string, number>; note?: string | null; version: number },
    idempotencyKey: string,
  ) =>
    request<ReceiveResult>(`/api/v1/requests/${requestId}/receipts`, {
      method: "POST",
      body: JSON.stringify(body),
      headers: { "Idempotency-Key": idempotencyKey },
    }),

  // ------------------------------------------------ 发票与匹配（8.3）

  /** 这张申请的发票（**含已作废的** —— 那是必须让人看见的事实）。 */
  listInvoices: (requestId: string) =>
    request<Invoice[]>(`/api/v1/requests/${requestId}/invoices`),

  /** 登记发票（可多张）。供应商必须与订单供应商一致。 */
  addInvoice: (
    requestId: string,
    body: {
      supplier_id: string;
      invoice_no: string;
      amount_fen: number;
      tax_amount_fen: number;
      issued_on?: string | null;
      attachment_id?: string | null;
      version: number;
    },
    idempotencyKey: string,
  ) =>
    request<InvoiceAdded>(`/api/v1/requests/${requestId}/invoices`, {
      method: "POST",
      body: JSON.stringify(body),
      headers: { "Idempotency-Key": idempotencyKey },
    }),

  /** 作废发票（改状态、留痕迹；作废后**不参与金额求和**，号可重新登记）。 */
  voidInvoice: (invoiceId: string, reason: string | null) =>
    request<InvoiceAdded>(`/api/v1/invoices/${invoiceId}/void`, {
      method: "POST",
      body: JSON.stringify({ reason }),
    }),

  /** 三单匹配预检（**只读**）。真正的拦截点在执行付款那一刻。 */
  match: (requestId: string) => request<MatchResult>(`/api/v1/requests/${requestId}/match`),

  // ------------------------------------------------ 付款与归档（8.4）

  /** 待付款队列（`INVOICED`）。`FINANCE`。
   *
   * ⚠️ `INVOICED` 的含义是"**可以进入付款流程**"，不是"付款条件已全部满足"——
   * 三单匹配还没跑（它跑在执行付款那一刻）。所以这是**待办清单**。 */
  paymentQueue: (params: { limit?: number; offset?: number } = {}) => {
    const query = new URLSearchParams();
    if (params.limit !== undefined) query.set("limit", String(params.limit));
    if (params.offset !== undefined) query.set("offset", String(params.offset));
    const suffix = query.toString() ? `?${query}` : "";
    return request<QueuePage>(`/api/v1/payments/queue${suffix}`);
  },

  /** 创建付款单（`PENDING`）。**只校验"已收票 + 有有效发票"，不跑匹配。** */
  createPayment: (requestId: string, idempotencyKey: string) =>
    request<PaymentCreated>(`/api/v1/requests/${requestId}/payments`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
    }),

  /** 执行付款（**触发三单匹配**）。条件更新抢付款权，抢输的一方 409。 */
  pay: (paymentId: string, version: number, idempotencyKey: string) =>
    request<PayResult>(`/api/v1/payments/${paymentId}/pay`, {
      method: "POST",
      body: JSON.stringify({ version }),
      headers: { "Idempotency-Key": idempotencyKey },
    }),

  /** 标记付款失败（**保留痕迹、允许重新发起**）。用于外部通道失败。 */
  failPayment: (paymentId: string, reason: string) =>
    request<PaymentCreated>(`/api/v1/payments/${paymentId}/fail`, {
      method: "POST",
      body: JSON.stringify({ reason }),
    }),

  /** 付款单列表（**含失败的尝试**）。 */
  listPayments: (requestId: string) =>
    request<Payment[]>(`/api/v1/requests/${requestId}/payments`),

  getPayment: (paymentId: string) => request<Payment>(`/api/v1/payments/${paymentId}`),

  /** 归档（`PAID → CLOSED`，终态）。 */
  closeRequest: (requestId: string, version: number) =>
    request<CloseResult>(`/api/v1/requests/${requestId}/close`, {
      method: "POST",
      body: JSON.stringify({ version }),
    }),
};

// ------------------------------------------------ 展示用文案

/** 报价状态的中文名。**与后端词表一一对应**（`domain/sourcing_policy`）。 */
export const QUOTATION_STATUS_LABEL: Record<QuotationStatus, string> = {
  SUBMITTED: "有效",
  EXPIRED: "已过期",
  SELECTED: "已成交",
  NOT_SELECTED: "未成交",
};

/** 付款状态的中文名（`domain/enums` 的 `PAYMENT_*`）。 */
export const PAYMENT_STATUS_LABEL: Record<PaymentStatus, string> = {
  PENDING: "待付款",
  PAID: "已付款",
  FAILED: "付款失败",
};

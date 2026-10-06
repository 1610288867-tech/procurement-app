/** 履约链（M10）的查询与写操作 hooks。
 *
 * ============================ 三条与后端对齐的地方 ============================
 * 1. **写操作成功后要让"申请详情 + 时间线"失效**：履约链的每一步都会写事件
 *    （`QUOTATION_ADDED` / `ORDER_PLACED` / `GOODS_RECEIVED` / `INVOICE_ADDED` /
 *    `INVOICE_VOIDED` / `PAYMENT_COMPLETED` …）。不失效的话时间线不会变，
 *    用户会以为操作没生效又点一次。
 * 2. **队列要一起失效**：定标之后那张单子要从"待询价"里消失、出现在"待收货"里。
 *    只失效详情页的话，工作台还挂着一条已经处理完的单子。
 * 3. **幂等键由调用方给**（`lib/idempotency.ts`）：同一次操作重试要复用同一把键。
 *    这里**不**在内部生成 —— 那样"重试"每次都是新键，等于没有幂等。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { fulfillmentApi, type Invoice } from "../fulfillment";

/** 与履约链相关的所有查询前缀。写操作成功后统一失效。 */
const FULFILLMENT_ROOTS = ["sourcing-queue", "receipt-queue", "payment-queue"] as const;

/** 一次写操作之后要刷新的东西：它自己那几张表 + 申请详情（含时间线）+ 三个队列。 */
function useFulfillmentInvalidate(requestId: string | null) {
  const client = useQueryClient();
  return () => {
    for (const root of FULFILLMENT_ROOTS) {
      client.invalidateQueries({ queryKey: [root] });
    }
    if (requestId) {
      client.invalidateQueries({ queryKey: ["requests", requestId] });
      client.invalidateQueries({ queryKey: ["compare", requestId] });
      client.invalidateQueries({ queryKey: ["order", requestId] });
      client.invalidateQueries({ queryKey: ["invoices", requestId] });
      client.invalidateQueries({ queryKey: ["match", requestId] });
      client.invalidateQueries({ queryKey: ["payments", requestId] });
    }
  };
}

// ------------------------------------------------ 队列

export function useSourcingQueue(enabled: boolean) {
  return useQuery({
    queryKey: ["sourcing-queue"],
    queryFn: () => fulfillmentApi.sourcingQueue({ limit: 50 }),
    enabled,
  });
}

export function useReceiptQueue(enabled: boolean) {
  return useQuery({
    queryKey: ["receipt-queue"],
    queryFn: () => fulfillmentApi.receiptQueue({ limit: 50 }),
    enabled,
  });
}

export function usePaymentQueue(enabled: boolean) {
  return useQuery({
    queryKey: ["payment-queue"],
    queryFn: () => fulfillmentApi.paymentQueue({ limit: 50 }),
    enabled,
  });
}

// ------------------------------------------------ 比价（8.1）

export function useCompare(requestId: string | null) {
  return useQuery({
    queryKey: ["compare", requestId ?? "none"],
    queryFn: () => fulfillmentApi.compare(requestId as string),
    enabled: requestId !== null,
  });
}

export function useAddQuotation(requestId: string) {
  const invalidate = useFulfillmentInvalidate(requestId);
  return useMutation({
    mutationFn: (body: {
      supplier_id: string;
      amount_fen: number;
      valid_until?: string | null;
    }) => fulfillmentApi.addQuotation(requestId, body),
    onSuccess: invalidate,
  });
}

export function useAward(requestId: string) {
  const invalidate = useFulfillmentInvalidate(requestId);
  return useMutation({
    mutationFn: (body: { supplier_id: string; version: number; idempotencyKey: string }) =>
      fulfillmentApi.award(
        requestId,
        { supplier_id: body.supplier_id, version: body.version },
        body.idempotencyKey,
      ),
    onSuccess: invalidate,
  });
}

// ------------------------------------------------ 订单与收货（8.2）

export function useOrder(requestId: string | null) {
  return useQuery({
    queryKey: ["order", requestId ?? "none"],
    queryFn: () => fulfillmentApi.orderOfRequest(requestId as string),
    enabled: requestId !== null,
  });
}

export function usePlaceOrder(requestId: string) {
  const invalidate = useFulfillmentInvalidate(requestId);
  return useMutation({
    mutationFn: (body: { version: number; idempotencyKey: string }) =>
      fulfillmentApi.placeOrder(requestId, body.version, body.idempotencyKey),
    onSuccess: invalidate,
  });
}

export function useReceive(requestId: string) {
  const invalidate = useFulfillmentInvalidate(requestId);
  return useMutation({
    mutationFn: (body: {
      accepted: Record<string, number>;
      note?: string | null;
      version: number;
      idempotencyKey: string;
    }) =>
      fulfillmentApi.receive(
        requestId,
        { accepted: body.accepted, note: body.note ?? null, version: body.version },
        body.idempotencyKey,
      ),
    onSuccess: invalidate,
  });
}

// ------------------------------------------------ 发票与匹配（8.3）

export function useInvoices(requestId: string | null) {
  return useQuery({
    queryKey: ["invoices", requestId ?? "none"],
    queryFn: () => fulfillmentApi.listInvoices(requestId as string),
    enabled: requestId !== null,
  });
}

export function useAddInvoice(requestId: string) {
  const invalidate = useFulfillmentInvalidate(requestId);
  return useMutation({
    mutationFn: (body: {
      supplier_id: string;
      invoice_no: string;
      amount_fen: number;
      tax_amount_fen: number;
      issued_on?: string | null;
      version: number;
      idempotencyKey: string;
    }) =>
      fulfillmentApi.addInvoice(
        requestId,
        {
          supplier_id: body.supplier_id,
          invoice_no: body.invoice_no,
          amount_fen: body.amount_fen,
          tax_amount_fen: body.tax_amount_fen,
          issued_on: body.issued_on ?? null,
          version: body.version,
        },
        body.idempotencyKey,
      ),
    onSuccess: invalidate,
  });
}

export function useVoidInvoice(requestId: string) {
  const invalidate = useFulfillmentInvalidate(requestId);
  return useMutation({
    mutationFn: (body: { invoiceId: string; reason: string | null }) =>
      fulfillmentApi.voidInvoice(body.invoiceId, body.reason),
    onSuccess: invalidate,
  });
}

/** 匹配预检。**只读**，但它是"能不能付款"的唯一权威答案 —— 所以轮询/重取都安全。 */
export function useMatch(requestId: string | null, enabled = true) {
  return useQuery({
    queryKey: ["match", requestId ?? "none"],
    queryFn: () => fulfillmentApi.match(requestId as string),
    enabled: requestId !== null && enabled,
  });
}

// ------------------------------------------------ 付款与归档（8.4）

export function usePayments(requestId: string | null) {
  return useQuery({
    queryKey: ["payments", requestId ?? "none"],
    queryFn: () => fulfillmentApi.listPayments(requestId as string),
    enabled: requestId !== null,
  });
}

export function useCreatePayment(requestId: string) {
  const invalidate = useFulfillmentInvalidate(requestId);
  return useMutation({
    mutationFn: (idempotencyKey: string) =>
      fulfillmentApi.createPayment(requestId, idempotencyKey),
    onSuccess: invalidate,
  });
}

export function usePay(requestId: string) {
  const invalidate = useFulfillmentInvalidate(requestId);
  return useMutation({
    mutationFn: (body: { paymentId: string; version: number; idempotencyKey: string }) =>
      fulfillmentApi.pay(body.paymentId, body.version, body.idempotencyKey),
    onSuccess: invalidate,
  });
}

export function useFailPayment(requestId: string) {
  const invalidate = useFulfillmentInvalidate(requestId);
  return useMutation({
    mutationFn: (body: { paymentId: string; reason: string }) =>
      fulfillmentApi.failPayment(body.paymentId, body.reason),
    onSuccess: invalidate,
  });
}

export function useCloseRequest(requestId: string) {
  const invalidate = useFulfillmentInvalidate(requestId);
  return useMutation({
    mutationFn: (body: { version: number }) =>
      fulfillmentApi.closeRequest(requestId, body.version),
    onSuccess: invalidate,
  });
}

// ------------------------------------------------ 纯计算（给界面用）

/** 有效（未作废）发票的金额之和与张数。
 *
 * ⚠️ **只在界面上做展示性汇总**，判断"能不能付款"一律用后端的匹配结果 ——
 * 前端自己算一遍就等于把"有效/作废"的判据抄了一份，抄的那份早晚会漂。
 */
export function activeInvoiceTotals(invoices: Invoice[]): { total: number; count: number } {
  return invoices.reduce(
    (acc, invoice) =>
      invoice.status === "ACTIVE"
        ? { total: acc.total + invoice.amount_fen, count: acc.count + 1 }
        : acc,
    { total: 0, count: 0 },
  );
}

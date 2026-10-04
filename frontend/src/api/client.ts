/** 前端统一 HTTP 客户端。
 *
 * 三条设计决定：
 *
 * 1. **BASE 是空字符串**（同源）。前端请求 /api/... 与 /health/...，由 Vite 的
 *    server.proxy 转发到 8000。所以不需要写死端口、不需要 CORS、
 *    也不需要 credentials: "include"——M3 引入 httpOnly Cookie 时这里一行都不用改。
 *
 * 2. **错误体形状与后端约定死**：
 *      { "error": { "code": "...", "message": "...", "request_id": "..." } }
 *    request_id 会带进错误对象——出错时把它贴出来，在后端日志里搜这 12 个字符
 *    就能还原整条请求链路。
 *
 * 3. **金额一律整数「分」**。所有 *_fen 字段都是分，界面上的"元"只在
 *    `src/lib/money.ts` 里换算——全项目只有那一个文件碰金额单位。
 *
 * 4. **access 令牌过期后静默续期**：access 只有 15 分钟。过期时先打一次
 *    `/auth/refresh` 换新令牌，再把原请求原样重放一遍 —— 用户感知不到，
 *    也不会被莫名其妙踢回登录页。**只重放一次**，续期失败就如实抛 401。
 */

const BASE = "";

export type ApiErrorBody = {
  error?: { code?: string; message?: string; request_id?: string };
};

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
    readonly requestId?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** 这三个路径自己不参与"401 → 续期 → 重放"：
 *  login 的 401 是密码错（不是令牌过期）；
 *  refresh 的 401 说明刷新令牌也没了，再递归问下去就是死循环；
 *  logout 无论如何都该成功结束。 */
const AUTH_PATHS = new Set([
  "/api/v1/auth/login",
  "/api/v1/auth/refresh",
  "/api/v1/auth/logout",
]);

function send(path: string, init: RequestInit): Promise<Response> {
  // ⚠️ `...init` **必须放在最前面**。
  // 对象字面量里后面的键会覆盖前面的：把 `...init` 写在 `headers` 之后，
  // 调用方传进来的 headers（例如 M5 的 Idempotency-Key）会把默认的
  // `Content-Type: application/json` 整个顶掉 —— 于是后端按表单去解析请求体，
  // 报一句 "Input should be a valid dictionary or object to extract fields from"（422），
  // 而前端看起来完全正常。实测踩过：只有带自定义头的接口会中招。
  //
  // ============================ 上传时不能带 Content-Type ============================
  // multipart 的 `Content-Type` 里必须带 boundary，而 boundary 是浏览器生成随机串时
  // 才知道的。所以传 `FormData` 时**一个字符都不能写**：
  //
  //     headers: { "Content-Type": "" }         ✗ 浏览器真的发一个空 Content-Type，
  //                                                后端 multipart 解析失败 → 422 "file: Field required"
  //     headers.delete("Content-Type")          ✓ 浏览器自己填 multipart/form-data; boundary=…
  //
  // **空字符串不等于"没设置"** —— 这条实测踩过（三种写法对比：empty-string → 422、
  // 不传 headers → 201、`Headers.delete()` → 201）。
  const headers = new Headers(init.headers);
  if (init.body instanceof FormData) {
    headers.delete("Content-Type");
  } else if (!headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  return fetch(`${BASE}${path}`, {
    ...init,
    // 同源时浏览器本来就会带 Cookie，写出来是为了 M10 前后端分源时不漏掉。
    // 另外它也是 M3 的 httpOnly Cookie 能生效的前提。
    credentials: "same-origin",
    headers,
  });
}

/** 同一时刻只允许有一次续期在飞。
 *
 * 页面一打开往往会并发好几个查询，它们会在同一瞬间一起拿到 401；
 * 不合并的话就会同时打四五次 `/auth/refresh`，既浪费又容易互相踩。
 */
let refreshing: Promise<boolean> | null = null;

function refreshAccessToken(): Promise<boolean> {
  if (!refreshing) {
    const started = (async () => {
      try {
        const response = await fetch(`${BASE}/api/v1/auth/refresh`, {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
        });
        return response.ok;
      } catch {
        // 后端不可达之类的情况：当作续期失败，让调用方看到原本的 401
        return false;
      }
    })();
    refreshing = started;
    // 收尾时把单飞标志清掉，下一次真的过期还能再续一次。
    // 这里用 doRefresh 自己的 promise，不返回它，避免调用方拿到未处理的拒绝。
    void started.finally(() => {
      if (refreshing === started) refreshing = null;
    });
  }
  return refreshing;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response = await send(path, init);

  if (response.status === 401 && !AUTH_PATHS.has(path)) {
    if (await refreshAccessToken()) {
      response = await send(path, init);
    }
  }

  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as ApiErrorBody;
    // 后端不可达时，Vite 代理会返回**空响应体的 500**，而不是连接错误。
    // 不特判的话页面只会显示"请求失败：500"，完全看不出是后端没启动。
    const unreachable = !body.error && [500, 502, 503, 504].includes(response.status);
    throw new ApiError(
      body.error?.message ??
        (unreachable
          ? `请求失败：${response.status} —— 后端可能没启动（先确认 uvicorn 在 8000 端口跑着）`
          : `请求失败：${response.status}`),
      response.status,
      body.error?.code,
      body.error?.request_id,
    );
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/**
 * 从 `Content-Disposition` 里取回文件名（M9 下载用）。
 *
 * 后端按 RFC 5987 发两个：
 *
 *     filename="dsh-probe-???.pdf"; filename*=UTF-8''dsh-probe-%E6%8A%A5%E4%BB%B7%E5%8D%95.pdf
 *
 * **优先取 `filename*`**：`filename=` 那份是给老客户端兜底的 ASCII 版，
 * 中文会被替换成 `?`（音译不了就只能这样），拿它当文件名等于把用户的中文名丢掉。
 *
 * 解析失败返回 null，由调用方回落到"列表里那个原始文件名" ——
 * 下载文件名不该成为一个能阻断下载的理由。
 */
export function filenameFromDisposition(header: string | null): string | null {
  if (!header) return null;

  const extended = /filename\*=UTF-8''([^;]+)/i.exec(header);
  if (extended?.[1]) {
    try {
      return decodeURIComponent(extended[1].trim());
    } catch {
      // 百分号编码残缺（理论上不会发生）：别在这里抛，往下走回落逻辑
    }
  }

  const plain = /filename="([^"]*)"/i.exec(header);
  if (plain?.[1]) return plain[1];
  return null;
}

// ------------------------------------------------------------------ 类型

export type HealthReady = { status: string; database: string };

/** 当前登录账号。**没有 password_hash** —— 后端也不该给。 */
export type Account = {
  id: string;
  username: string;
  full_name: string;
  department_id: string | null;
  roles: string[];
};

export type Supplier = {
  id: string;
  name: string;
  code: string;
  active: boolean;
  qualification_expires_at: string | null;
};

export type Department = {
  id: string;
  name: string;
  code: string;
  parent_id: string | null;
  active: boolean;
};

export type ApprovalRole =
  | "DEPT_MANAGER"
  | "DEPT_HEAD"
  | "PROCUREMENT"
  | "FINANCE"
  | "GM";

export type ApprovalStep = {
  order: number;
  role: ApprovalRole;
  mode: "SINGLE" | "ALL";
  /** 受限 DSL；金额字段叫 amount_fen（单位：分） */
  when: Record<string, unknown>;
};

export type ApprovalPolicy = {
  steps: ApprovalStep[];
  skip_when_framework_contract?: boolean;
  urgency_extra_step?: ApprovalStep | null;
};

export type Category = {
  id: string;
  name: string;
  code: string;
  form_schema: Record<string, unknown>;
  approval_policy: ApprovalPolicy;
  version: number;
  active: boolean;
};

export type Budget = {
  id: string;
  department_id: string;
  category_id: string;
  year: number;
  amount_total_fen: number;
  amount_reserved_fen: number;
  amount_spent_fen: number;
};

/** 看板行 = 预算 + 部门名 + 品类名。
 *  名字由后端带出来：财务没有"列出全部部门 / 品类"的权限（那是管理员接口）。 */
export type BudgetBoardRow = Budget & {
  department_name: string;
  category_name: string;
};

/** 采购申请状态（M5 只用到这四个；后九个状态随 M6–M9 加）。 */
export type RequestStatus =
  | "DRAFT"
  | "IN_APPROVAL"
  | "DISCARDED"
  | "WITHDRAWN"
  | "SOURCING"
  | "REJECTED";

export type RequestUrgency = "NORMAL" | "URGENT";

/** 申请主单。金额一律「分」—— 名字里带 _fen，界面上的「元」只在 money.ts 换算。 */
export type PurchaseRequest = {
  id: string;
  code: string;
  requester_id: string;
  department_id: string;
  category_id: string;
  title: string;
  reason: string | null;
  amount_total_fen: number;
  urgency: RequestUrgency;
  status: RequestStatus;
  version: number;
  form_data: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  submitted_at: string | null;
  closed_at: string | null;
};

export type PurchaseItem = {
  id: string;
  line_no: number;
  name: string;
  spec: string | null;
  quantity: number;
  unit_price_fen: number;
  amount_fen: number;
};

export type RequestEvent = {
  id: string;
  event_type: string;
  from_status: RequestStatus | null;
  to_status: RequestStatus | null;
  actor_id: string;
  payload: Record<string, unknown>;
  created_at: string;
};

export type RequestDetail = {
  request: PurchaseRequest;
  items: PurchaseItem[];
  timeline: RequestEvent[];
};

// ------------------------------------------------ 附件（M9）

/** 附件用途。是**给人看的分类**，不参与任何校验判定。 */
export type AttachmentKind =
  | "QUOTATION"
  | "CONTRACT"
  | "INVOICE"
  | "RECEIPT"
  | "OTHER";

export const ATTACHMENT_KIND_LABEL: Record<AttachmentKind, string> = {
  QUOTATION: "报价单",
  CONTRACT: "合同",
  INVOICE: "发票",
  RECEIPT: "验收单",
  OTHER: "其他",
};

/** 附件元数据。
 *
 * **没有 `storage_key`**：那是服务端的存储细节，后端刻意不返回它。
 * 磁盘上的文件名由服务端生成（UUID），与 `original_filename` 无关 ——
 * 后者只用来显示。
 */
export type Attachment = {
  id: string;
  request_id: string;
  kind: AttachmentKind;
  original_filename: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
  uploaded_by: string;
  created_at: string;
};

export type ItemInput = {
  name: string;
  spec?: string | null;
  quantity: number;
  /** 单价，单位：分（界面填「元」，由 money.ts 换算） */
  unit_price_fen: number;
};

// ------------------------------------------------ 审批（M6）

/** 审批任务状态。

 * **刻意没有"还没轮到"这一档**：那件事由审批流的 `current_step_order` 表达。
 * 同一个概念存两处（流说第 3 级、任务说自己在等）必然漂移，所以任务词表保持五档，
 * 界面上的"未开始 ○"是**算出来的**，不是另一个状态。
 */
export type ApprovalTaskStatus =
  | "PENDING"
  | "APPROVED"
  | "REJECTED"
  | "SKIPPED"
  | "CANCELLED";

export type ApprovalMode = "SINGLE" | "ALL";

export type ApprovalTask = {
  id: string;
  request_id: string;
  step_order: number;
  role: ApprovalRole;
  mode: ApprovalMode;
  status: ApprovalTaskStatus;
  /** NULL = 未认领（只有或签会这样）；会签在建任务时就预分配 */
  assignee_id: string | null;
  decided_by: string | null;
  decided_at: string | null;
  comment: string | null;
  version: number;
  created_at: string;
};

/** 待办列表里的申请摘要（金额、申请人、品类一次 JOIN 带出）。 */
export type RequestSummary = {
  id: string;
  code: string;
  title: string;
  requester_id: string;
  category_id: string;
  category_name: string;
  amount_total_fen: number;
  urgency: RequestUrgency;
  status: RequestStatus;
  submitted_at: string | null;
};

export type TodoItem = { task: ApprovalTask; request: RequestSummary };

export type TaskDetail = {
  task: ApprovalTask;
  request: RequestSummary;
  /** 审批链全貌：界面上的"已完成 / 当前 / 未开始"由它 + current_step_order 推出 */
  steps: ApprovalTask[];
  current_step_order: number;
  /** 这条任务分配给谁（未认领时为 null） */
  assignee_name: string | null;
};

export type ApprovalFlow = {
  request_id: string;
  status: "RUNNING" | "APPROVED" | "REJECTED" | "CANCELLED";
  current_step_order: number;
  /** 提交那一刻的策略原件 —— 事后解释"为什么只走了两级"的依据 */
  policy_snapshot: Record<string, unknown>;
  policy_version: number;
  started_at: string;
  completed_at: string | null;
  steps: ApprovalTask[];
};

export type DecisionResult = {
  approved: boolean;
  flow_status: string;
  request_status: RequestStatus;
  step_completed: boolean | null;
  current_step_order: number | null;
  comment: string | null;
};

// ------------------------------------------------ 预算看板（M7）

/** 预算流水类型。三类合起来必须能还原预算的每一次变动；
 *  `RELEASE` 有两种来源：驳回/撤回的全额释放，以及付款时"预留 − 实付"的差额。 */
export type BudgetTransactionKind = "RESERVE" | "RELEASE" | "SPEND";

export type BudgetTransaction = {
  id: string;
  budget_id: string;
  request_id: string;
  request_code: string;
  kind: BudgetTransactionKind;
  /** 永远是正数，方向由 kind 表达 */
  amount_fen: number;
  created_at: string;
};

// ------------------------------------------------------------------ 接口

export const api = {
  live: () => request<{ status: string }>("/health/live"),
  ready: () => request<HealthReady>("/health/ready"),

  // 认证（M3）。令牌在 httpOnly Cookie 里，前端**读不到也不算不碰**。
  me: () => request<Account>("/api/v1/auth/me"),
  login: (body: { username: string; password: string }) =>
    request<Account>("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  logout: () => request<void>("/api/v1/auth/logout", { method: "POST" }),

  // 公开只读
  listSuppliers: () => request<Supplier[]>("/api/v1/suppliers"),
  listDepartments: () => request<Department[]>("/api/v1/departments"),
  listCategories: () => request<Category[]>("/api/v1/categories"),

  // 管理员（M3 起会带权限）
  adminListSuppliers: () => request<Supplier[]>("/api/v1/admin/suppliers"),
  adminCreateSupplier: (body: {
    name: string;
    code: string;
    qualification_expires_at?: string | null;
  }) => request<Supplier>("/api/v1/admin/suppliers", { method: "POST", body: JSON.stringify(body) }),
  adminDisableSupplier: (id: string, reason: string) =>
    request<{ ok: boolean }>(`/api/v1/admin/suppliers/${id}/disable`, {
      method: "POST",
      body: JSON.stringify({ reason }),
    }),

  adminCreateDepartment: (body: { name: string; code: string }) =>
    request<Department>("/api/v1/admin/departments", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  adminListCategories: () => request<Category[]>("/api/v1/admin/categories"),
  adminCreateCategory: (body: {
    name: string;
    code: string;
    form_schema?: Record<string, unknown>;
    approval_policy: ApprovalPolicy;
  }) =>
    request<Category>("/api/v1/admin/categories", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  adminUpdateApprovalPolicy: (id: string, approvalPolicy: ApprovalPolicy) =>
    request<Category>(`/api/v1/admin/categories/${id}/approval-policy`, {
      method: "PUT",
      body: JSON.stringify({ approval_policy: approvalPolicy }),
    }),

  adminListBudgets: () => request<Budget[]>("/api/v1/admin/budgets"),
  adminCreateBudget: (body: {
    department_id: string;
    category_id: string;
    year: number;
    amount_fen: number;
  }) =>
    request<Budget>("/api/v1/admin/budgets", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // ------------------------------------------------ 采购申请（M5）
  listRequests: (params: { status?: RequestStatus; limit?: number; offset?: number } = {}) => {
    const query = new URLSearchParams();
    if (params.status) query.set("status", params.status);
    if (params.limit !== undefined) query.set("limit", String(params.limit));
    if (params.offset !== undefined) query.set("offset", String(params.offset));
    const suffix = query.toString() ? `?${query}` : "";
    return request<{ items: PurchaseRequest[]; total: number }>(
      `/api/v1/requests${suffix}`,
    );
  },
  getRequest: (id: string) => request<RequestDetail>(`/api/v1/requests/${id}`),
  createRequest: (body: {
    category_id: string;
    title: string;
    reason?: string | null;
    urgency?: RequestUrgency;
    form_data?: Record<string, unknown>;
  }) =>
    request<PurchaseRequest>("/api/v1/requests", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  patchRequest: (
    id: string,
    body: {
      version: number;
      title?: string;
      reason?: string | null;
      urgency?: RequestUrgency;
      category_id?: string;
      form_data?: Record<string, unknown>;
    },
  ) =>
    request<PurchaseRequest>(`/api/v1/requests/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  replaceItems: (id: string, version: number, items: ItemInput[]) =>
    request<PurchaseRequest>(`/api/v1/requests/${id}/items`, {
      method: "PUT",
      body: JSON.stringify({ version, items }),
    }),
  /** 提交 / 撤回：**必须带幂等键**（重复执行会改变结果的动作）。 */
  submitRequest: (id: string, version: number, idempotencyKey: string) =>
    request<PurchaseRequest>(`/api/v1/requests/${id}/submit`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({ version }),
    }),
  withdrawRequest: (id: string, version: number, idempotencyKey: string) =>
    request<PurchaseRequest>(`/api/v1/requests/${id}/withdraw`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({ version }),
    }),
  discardRequest: (id: string, version: number) =>
    request<PurchaseRequest>(`/api/v1/requests/${id}/discard`, {
      method: "POST",
      body: JSON.stringify({ version }),
    }),

  // ------------------------------------------------ 审批（M6）
  /** 我的待办。**后端只返回"当前这一级"** —— 还没轮到的高级别不会出现。 */
  listTodo: (params: { limit?: number; offset?: number } = {}) => {
    const query = new URLSearchParams();
    if (params.limit !== undefined) query.set("limit", String(params.limit));
    if (params.offset !== undefined) query.set("offset", String(params.offset));
    const suffix = query.toString() ? `?${query}` : "";
    return request<{ items: TodoItem[]; total: number }>(
      `/api/v1/approvals/tasks${suffix}`,
    );
  },
  getTask: (id: string) => request<TaskDetail>(`/api/v1/approvals/tasks/${id}`),
  /** 认领（抢单）。只有未认领的**或签**任务能被认领：会签建单时就指定了人。 */
  claimTask: (id: string, version: number) =>
    request<ApprovalTask>(`/api/v1/approvals/tasks/${id}/claim`, {
      method: "POST",
      body: JSON.stringify({ version }),
    }),
  approveTask: (id: string, version: number, comment?: string | null) =>
    request<DecisionResult>(`/api/v1/approvals/tasks/${id}/approve`, {
      method: "POST",
      body: JSON.stringify({ version, comment: comment || null }),
    }),
  /** 驳回：**意见必填** —— 它是申请人唯一能看到的理由。 */
  rejectTask: (id: string, version: number, comment: string) =>
    request<DecisionResult>(`/api/v1/approvals/tasks/${id}/reject`, {
      method: "POST",
      body: JSON.stringify({ version, comment }),
    }),
  /** 审批链进度：申请人与审批人都能看（卡在谁那里一眼可见）。 */
  getRequestFlow: (requestId: string) =>
    request<ApprovalFlow>(`/api/v1/approvals/requests/${requestId}/flow`),

  // ------------------------------------------------ 预算看板（M7，只读）
  /** 全部预算（财务 / 管理员）。**只读** —— 改预算仍然只走 /admin/budgets。 */
  listBudgets: (params: { year?: number } = {}) => {
    const query = new URLSearchParams();
    if (params.year !== undefined) query.set("year", String(params.year));
    const suffix = query.toString() ? `?${query}` : "";
    return request<BudgetBoardRow[]>(`/api/v1/budgets${suffix}`);
  },
  /** 预算流水（新→旧），带申请单号，可直接当对账依据看。 */
  listBudgetTransactions: (params: { budgetId?: string; limit?: number } = {}) => {
    const query = new URLSearchParams();
    if (params.budgetId) query.set("budget_id", params.budgetId);
    if (params.limit !== undefined) query.set("limit", String(params.limit));
    const suffix = query.toString() ? `?${query}` : "";
    return request<BudgetTransaction[]>(`/api/v1/budgets/transactions${suffix}`);
  },

  // ------------------------------------------------ 附件（M9）

  /** 某张申请的附件列表（新的在前）。 */
  listAttachments: (requestId: string) =>
    request<Attachment[]>(`/api/v1/requests/${requestId}/attachments`),

  /** 上传附件。
   *
   * ============================ 两个必须这样做的地方 ============================
   * 1. **不要自己写 `Content-Type`**：multipart 的 `Content-Type` 里要带 boundary，
   *    而 boundary 是浏览器生成随机串时才知道的。`send()` 已经会为 `FormData`
   *    把默认的 `Content-Type: application/json` 删掉，所以这里**什么都不用传** ——
   *    传 `""` 是错的（浏览器会真的发一个空头，后端解析失败报 422 "file: Field required"）。
   * 2. **`File` 原样交给 FormData**：`filename` 由浏览器填，中文名会被正确编码。
   */
  uploadAttachment: (requestId: string, file: File, kind: AttachmentKind) => {
    const body = new FormData();
    body.append("file", file);
    body.append("kind", kind);
    return request<Attachment>(`/api/v1/requests/${requestId}/attachments`, {
      method: "POST",
      body,
    });
  },

  /** 下载附件并触发浏览器保存。
   *
   * 为什么不用 `<a href="/api/v1/attachments/…">` 直接指过去：
   * 那样**拿不到错误**（403/404 会在新标签页里显示一段 JSON，
   * 用户只会看到一句"无权访问"而不知道发生了什么），也没法统一走
   * 客户端已有的 401 → 续期 → 重放逻辑。这里先取成 Blob，成功才落盘。
   */
  downloadAttachment: async (attachmentId: string, fallbackName: string) => {
    const response = await fetch(`${BASE}/api/v1/attachments/${attachmentId}/download`, {
      credentials: "same-origin",
    });
    if (!response.ok) {
      const body = (await response.json().catch(() => ({}))) as ApiErrorBody;
      throw new ApiError(
        body.error?.message ?? `下载失败：${response.status}`,
        response.status,
        body.error?.code,
        body.error?.request_id,
      );
    }
    const blob = await response.blob();
    const name =
      filenameFromDisposition(response.headers.get("Content-Disposition")) ?? fallbackName;

    const url = URL.createObjectURL(blob);
    try {
      const link = document.createElement("a");
      link.href = url;
      link.download = name;
      document.body.appendChild(link);
      link.click();
      link.remove();
    } finally {
      // 不释放就是内存泄漏：Blob URL 会一直把内容留在内存里直到页面关闭
      URL.revokeObjectURL(url);
    }
  },
};

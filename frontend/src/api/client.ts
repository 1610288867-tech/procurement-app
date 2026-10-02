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

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...(init.headers ?? {}) },
    ...init,
  });

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

// ------------------------------------------------------------------ 类型

export type HealthReady = { status: string; database: string };

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

// ------------------------------------------------------------------ 接口

export const api = {
  live: () => request<{ status: string }>("/health/live"),
  ready: () => request<HealthReady>("/health/ready"),

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
};

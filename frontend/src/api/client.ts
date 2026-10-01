/** 前端统一 HTTP 客户端。
 *
 * 两条设计决定：
 *
 * 1. **BASE 是空字符串**（同源）。前端请求 /api/...，由 Vite 的 server.proxy
 *    转发到 8000。所以不需要写死端口、不需要 CORS、不需要 credentials: "include"
 *    ——M3 引入 httpOnly Cookie 时，这个文件一行都不用改。
 *
 * 2. **错误体形状与后端约定死**：
 *      { "error": { "code": "...", "message": "...", "request_id": "..." } }
 *    request_id 会被拼进错误消息——出错时你把它贴给我，我在后端日志里
 *    搜这 12 个字符就能还原整条请求链路。
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
    const message = body.error?.message ?? `请求失败：${response.status}`;
    throw new ApiError(
      message,
      response.status,
      body.error?.code,
      body.error?.request_id,
    );
  }

  // 204 没有响应体
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/** 后端健康检查的响应形状。 */
export type HealthReady = { status: string; database: string };

export const api = {
  live: () => request<{ status: string }>("/health/live"),
  ready: () => request<HealthReady>("/health/ready"),
};

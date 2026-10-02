/** 管理页共用的小组件：卡片容器、表单错误、输入框样式。
 *
 * 抽出来只有一个目的：三个管理页长得一样，错误提示的处理方式也一样 ——
 * **后端 422 的 message 要原样显示**，它是管理员唯一能自救的线索。
 */

import type { ReactNode } from "react";

import { ApiError } from "../../api/client";

export const inputClass =
  "mt-1 rounded border border-slate-300 px-2 py-1 text-slate-900";

export function Section({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-6 shadow-sm">
      <h2 className="mb-3 text-lg font-semibold text-slate-800">{title}</h2>
      {children}
    </div>
  );
}

/** 把后端返回的错误原样展示；带 request_id 时一并给出，方便对日志。 */
export function FormError({ error }: { error: unknown }) {
  if (!error) return null;
  const message = (error as Error).message;
  const requestId = error instanceof ApiError ? error.requestId : undefined;
  return (
    <p className="mt-3 whitespace-pre-wrap text-sm text-red-600">
      {message}
      {requestId && (
        <span className="ml-1 text-slate-400">（request_id: {requestId}）</span>
      )}
    </p>
  );
}

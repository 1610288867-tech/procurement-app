/** 附件的查询、上传、删除 hooks（M9）。
 *
 * 四条与后端对齐的地方：
 *
 * 1. **上传成功后要让详情页失效**：不失效的话时间线里不会出现"上传附件"那一条
 *    （它是服务端写的事件），用户会以为没生效。
 * 2. **删除同理**：时间线会多一条 `ATTACHMENT_REMOVED`，而且附件要从列表里消失。
 * 3. **下载不在这里**：它是一次性动作、不是状态，用 `api.downloadAttachment`
 *    直接调即可；包成 mutation 只会多一层没用的 loading 状态。
 * 4. **`kind` 必传**：后端缺省落 `OTHER`，但界面既然让用户选，就把选择传下去。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, type AttachmentKind } from "../client";

const ATTACHMENTS = ["attachments"] as const;

const listKey = (requestId: string) => [...ATTACHMENTS, requestId] as const;

export function useAttachments(requestId: string | null) {
  return useQuery({
    queryKey: listKey(requestId ?? "none"),
    queryFn: () => api.listAttachments(requestId as string),
    enabled: requestId !== null,
  });
}

export function useUploadAttachment(requestId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({
      file,
      kind,
      progressToken,
    }: {
      file: File;
      kind: AttachmentKind;
      /** 给了就沿途报进度（见 `client.ts` 的进度注册表） */
      progressToken?: string;
    }) => api.uploadAttachment(requestId, file, kind, progressToken),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: listKey(requestId) });
      // 时间线多了一条 ATTACHMENT_ADDED：详情页必须跟着刷新，
      // 否则"传完了但时间线没变"会让人以为上传失败又重传一次。
      client.invalidateQueries({ queryKey: ["requests", requestId] });
    },
  });
}

export function useDeleteAttachment(requestId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (attachmentId: string) => api.deleteAttachment(attachmentId),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: listKey(requestId) });
      // 删除同样会写事件（ATTACHMENT_REMOVED），所以时间线也要刷新
      client.invalidateQueries({ queryKey: ["requests", requestId] });
    },
  });
}

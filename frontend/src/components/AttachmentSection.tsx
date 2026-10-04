/** 附件区（M9）：上传 / 用途 / 列表 / 下载。
 *
 * ============================ 界面能做的校验，与后端必须做的校验 ============================
 * 这里会做两条**便宜**的预检（大小、扩展名），目的是让用户**当场**知道
 * "这个文件不行"，而不是等上传完再弹一个 422：
 *
 *     文件 > 10 MB          → 直接提示，不发请求
 *     扩展名不在白名单       → 直接提示，不发请求
 *
 * 但**真正的判定永远在后端**（六步校验：大小 → 类型 → magic bytes → 真解码 → 摘要）。
 * 前端的预检只是体验：改个扩展名就能绕过它 —— 而后端不会被绕过，
 * 那才是边界所在（`domain/attachments.py` 的模块注释写了每一层挡什么）。
 *
 * ============================ 上传失败要能看懂 ============================
 * 后端 422 的文案是**给人看的**（"文件内容与声明的类型不符：只改扩展名是骗不过去的"、
 * "图片尺寸过大：20000×20000"…），所以这里**原样显示 `ApiError.message`**，
 * 不包装成"上传失败，请重试" —— 那等于把最有用的信息丢掉。
 */

import { useRef, useState } from "react";

import {
  ATTACHMENT_KIND_LABEL,
  ApiError,
  type Attachment,
  type AttachmentKind,
} from "../api/client";
import { useAttachments, useUploadAttachment } from "../api/hooks/useAttachments";
import { api } from "../api/client";
import { Button, ErrorNote, Loading, Select } from "./ui";

/** 与后端 `domain/attachments.py` 的上限一致。**改一处就要改两处**，所以放在这里显眼处。 */
const MAX_BYTES = 10 * 1024 * 1024;

/** 与后端 `EXTENSION_TYPES` 一致：只用于"当场提示"，不构成安全边界。 */
const ALLOWED_EXTENSIONS = [".pdf", ".jpg", ".jpeg", ".png"];

const KIND_OPTIONS = Object.entries(ATTACHMENT_KIND_LABEL) as [AttachmentKind, string][];

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function extensionOf(filename: string): string {
  const index = filename.lastIndexOf(".");
  return index > 0 ? filename.slice(index).toLowerCase() : "";
}

export function AttachmentSection({
  requestId,
  /** 已提交的单子才允许上传？—— 不限制：报价单往往**提交之后**才有。
   *  但草稿页与详情页的措辞不同，所以目录文案由调用方给。 */
  canUpload = true,
}: {
  requestId: string;
  canUpload?: boolean;
}) {
  const attachments = useAttachments(requestId);
  const upload = useUploadAttachment(requestId);

  const fileInput = useRef<HTMLInputElement | null>(null);
  const [kind, setKind] = useState<AttachmentKind>("QUOTATION");
  const [localError, setLocalError] = useState<string | null>(null);
  const [downloading, setDownloading] = useState<string | null>(null);

  const rows = attachments.data ?? [];

  function pickFile() {
    setLocalError(null);
    fileInput.current?.click();
  }

  function onFileChosen(input: HTMLInputElement) {
    const file = input.files?.[0];
    // 先把 input 清空再处理：同一个文件连传两次时 value 不变就不会再触发 change
    // （用户会以为"点了没反应"）。放在最前面，两条预检提前返回时也一并清掉了。
    input.value = "";
    if (!file) return;
    setLocalError(null);

    // 两条便宜预检（见文件头：只是体验，不是边界）
    if (file.size > MAX_BYTES) {
      setLocalError(
        `文件 ${formatSize(file.size)} 超过 10 MB 上限 —— 请压缩或拆分后重试。`,
      );
      return;
    }
    const extension = extensionOf(file.name);
    if (!ALLOWED_EXTENSIONS.includes(extension)) {
      setLocalError(
        `只接受 ${ALLOWED_EXTENSIONS.join(" / ")}，当前是「${extension || "无扩展名"}」。`,
      );
      return;
    }

    upload.mutate({ file, kind });
  }

  async function download(row: Attachment) {
    setLocalError(null);
    setDownloading(row.id);
    try {
      await api.downloadAttachment(row.id, row.original_filename);
    } catch (error) {
      setLocalError(
        error instanceof ApiError ? error.message : "下载失败，请稍后重试。",
      );
    } finally {
      setDownloading(null);
    }
  }

  return (
    <div className="space-y-4" data-testid="attachment-section">
      {canUpload && (
        <div className="flex flex-wrap items-end gap-2">
          <label className="text-sm text-body">
            <span className="mb-1 block text-xs text-muted">用途</span>
            <Select
              value={kind}
              onChange={(event) => setKind(event.target.value as AttachmentKind)}
              aria-label="附件用途"
            >
              {KIND_OPTIONS.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </Select>
          </label>

          <Button
            type="button"
            onClick={pickFile}
            disabled={upload.isPending}
            data-testid="attachment-upload-button"
          >
            {upload.isPending ? "上传中…" : "选择文件上传"}
          </Button>

          <input
            ref={fileInput}
            type="file"
            className="hidden"
            accept={ALLOWED_EXTENSIONS.join(",")}
            data-testid="attachment-file-input"
            onChange={(event) => onFileChosen(event.currentTarget)}
          />

          <p className="text-xs text-faint">PDF / JPG / PNG，单个不超过 10 MB</p>
        </div>
      )}

      {localError && <ErrorNote error={new Error(localError)} />}
      {upload.isError && <ErrorNote error={upload.error} />}

      {attachments.isPending && <Loading label="加载附件…" />}
      <ErrorNote error={attachments.error} />

      {attachments.data && rows.length === 0 && (
        <p className="text-sm text-muted">
          还没有附件。{canUpload ? "报价单、合同、发票都可以传。" : ""}
        </p>
      )}

      {rows.length > 0 && (
        <ul className="divide-y divide-line" data-testid="attachment-list">
          {rows.map((row) => (
            <li key={row.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2.5">
              <span className="rounded-sm bg-section px-2 py-0.5 text-xs text-muted">
                {ATTACHMENT_KIND_LABEL[row.kind] ?? row.kind}
              </span>
              <span className="min-w-0 flex-1 truncate text-sm text-ink" title={row.original_filename}>
                {row.original_filename}
              </span>
              <span className="font-mono text-xs text-faint">{formatSize(row.size_bytes)}</span>
              <span className="font-mono text-xs text-faint">
                {row.created_at.slice(0, 16).replace("T", " ")}
              </span>
              <button
                type="button"
                className="text-xs text-primary underline underline-offset-2 disabled:opacity-50"
                onClick={() => download(row)}
                disabled={downloading === row.id}
              >
                {downloading === row.id ? "下载中…" : "下载"}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

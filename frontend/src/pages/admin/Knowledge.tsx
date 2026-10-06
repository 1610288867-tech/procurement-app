/** 知识库管理（M11，仅 ADMIN）：上传制度文档、看版本与生效时间、**展开看切分结果**。
 *
 * ============================ 这一页真正要回答的问题 ============================
 * "助手答不出来"只有两类原因，而它们的修法完全相反：
 *
 *   1. **没检索到** —— 制度里确实没写，或者问法与制度用词对不上（切分没问题，是召回问题）；
 *   2. **切碎了 / 没切出来** —— 一段完整的规定被拦腰截断，或者压根没进库（`chunk_count` 为 0）。
 *
 * 只看一个"已就绪"的徽标永远分不清这两类。所以这一页把**每一片的分片号、字符区间与原文**
 * 摊开给人看：点开一行就知道它是没切出来、还是切坏了 —— 这是排查检索问题的第一站。
 *
 * ============================ 三条与后端对齐的地方 ============================
 * 1. **`source_key` 才是这份制度的身份，标题只是显示名**：检索按 `source_key` 分组，
 *    取"当前生效（`effective_from <= now`）里 `version` 最大的那一版"。
 *    所以改标题不算换制度，改 `source_key` 才算 —— 输入框下面专门写了这句话，
 *    因为这是一个按错一次就会让检索静默失效的字段（旧文档还在，新文档检索不到）。
 * 2. **`version` + `effective_from` 不是装饰**：没有它们，制度修订后新旧条文会同时被检索到，
 *    把已经作废的规定与现行规定一起引给用户 —— 对一个制度助手来说，这比查不到更糟。
 * 3. **`FAILED` 必须把 `failure_reason` 显示出来**：只给一个红徽标的话，
 *    上传失败就成了一件无法自救的事（用户不知道该改什么、也说不清哪里不对）。
 *
 * ============================ 上传走 `request()`，不自己写 fetch ============================
 * multipart 的 `Content-Type` 里必须带 boundary，而 boundary 只有浏览器生成随机串时才知道 ——
 * `request()` 已经为 `FormData` 处理了这件事（见 `client.ts` 里那段注释），
 * 所以这里**一个字节的 header 都不能自己设**。
 * 代价是没有上传进度：这一页不加进度条的判据是"阻塞与否"——制度文档不大，
 * 上传期间按钮上的"上传中…"够用了；附件那边之所以要进度，是因为 10 MB 的扫描件要几十秒。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useState, type FormEvent } from "react";

import { request } from "../../api/client";
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
  Row,
  Table,
} from "../../components/ui";

// ================================================================ 类型（与后端形状一一对应）

/** 一份制度文档（列表项）。
 *
 * `status` 的三档**与数据库 CHECK 约束同源**（`ck_knowledge_doc_status_known`）。
 * `failure_reason` 与 `filename` 在后端是可空的（列本身 nullable），所以这里也是 `| null`。
 */
type KnowledgeDoc = {
  id: string;
  source_key: string;
  title: string;
  version: number;
  effective_from: string;
  filename: string | null;
  status: string;
  embedding_model: string;
  chunk_count: number;
  content_chars: number;
  failure_reason: string | null;
  created_at: string;
};

/** 一个切分片段。字符区间是**相对原文**的偏移，出问题时用它回原文定位。 */
type KnowledgeChunk = {
  chunk_no: number;
  content: string;
  start_offset: number;
  end_offset: number;
  length: number;
};

/** 上传表单拼出来的请求。`effective_from` 留空 = 让服务端按"立即生效"处理。 */
type UploadInput = {
  file: File;
  title: string;
  source_key: string;
  version: number;
  effective_from: string | null;
};

/** 状态 → 中文与配色。写法照 `ui.tsx` 里的 `REQUEST_STATUS_STYLE`：
 *  键是开放集合（后端将来加状态时前端只是显示成原文，不会崩）。 */
const DOC_STATUS_STYLE: Record<
  string,
  { label: string; tone: "neutral" | "success" | "warning" | "danger" }
> = {
  READY: { label: "已就绪", tone: "success" },
  PENDING: { label: "处理中", tone: "warning" },
  FAILED: { label: "失败", tone: "danger" },
};

/** 表头与展开行的 `colSpan` **共用这一个数组**：加一列时不会忘掉那个数字。 */
const DOC_COLUMNS = [
  "标题",
  "source_key",
  "版本",
  "生效时间",
  "状态",
  "分片数",
  "向量模型",
  "",
];

const DOCS_KEY = ["knowledge-docs"] as const;

// ================================================================ 接口

/** 文档列表（新的在前，顺序由服务端给）。 */
function listDocs(): Promise<KnowledgeDoc[]> {
  return request<KnowledgeDoc[]>("/api/v1/admin/knowledge-docs");
}

/** 某份文档的切分结果 —— 展开那一行时才拉。 */
function listChunks(docId: string): Promise<KnowledgeChunk[]> {
  return request<KnowledgeChunk[]>(`/api/v1/admin/knowledge-docs/${docId}/chunks`);
}

/** 上传（multipart）。
 *
 * 注意两件事：
 *   - **不设 `Content-Type`**：boundary 由浏览器生成，手写（哪怕写空串）都会让后端
 *     解析失败并报 `file: Field required`（`client.ts` 里记了这次实测）。
 *   - 上传返回什么后端没在接口形状里写死：这里按"返回这条文档"写，但**页面不依赖它** ——
 *     列表一律靠 `invalidateQueries` 重拉（那才是唯一可信的读数）。
 */
function uploadDoc(input: UploadInput): Promise<KnowledgeDoc> {
  const body = new FormData();
  body.append("file", input.file);
  body.append("title", input.title);
  body.append("source_key", input.source_key);
  body.append("version", String(input.version));
  if (input.effective_from) body.append("effective_from", input.effective_from);
  return request<KnowledgeDoc>("/api/v1/admin/knowledge-docs", { method: "POST", body });
}

// ================================================================ 数据 hooks
// 这两个接口目前只有这一页用，所以就近放着；等第二个页面也要用时再抽到 `api/hooks/`。

function useKnowledgeDocs() {
  return useQuery({
    queryKey: DOCS_KEY,
    queryFn: listDocs,
    // `PENDING` 是**暂时的**（正在切分与向量化）。不轮询的话用户会盯着一个永远不动的
    // "处理中"，然后自己猜要不要刷新 —— 而"要不要刷新"本来就不该由用户猜。
    // 没有 PENDING 时完全不轮询。
    refetchInterval: (query) =>
      (query.state.data ?? []).some((doc) => doc.status === "PENDING") ? 3000 : false,
  });
}

function useKnowledgeChunks(docId: string) {
  return useQuery({
    queryKey: ["knowledge-chunks", docId],
    queryFn: () => listChunks(docId),
  });
}

function useUploadDoc() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: uploadDoc,
    // 上传成功后**必须重拉列表**：这条文档的状态、分片数、向量模型只有列表里有。
    // 不重拉的话用户会以为没传上去，然后再传一遍 —— 而同一 source_key + version
    // 会被唯一约束挡回 409（那不是"重试"，是"重复上传"）。
    onSuccess: () => client.invalidateQueries({ queryKey: DOCS_KEY }),
  });
}

// ================================================================ 页面

export default function AdminKnowledge() {
  const docs = useKnowledgeDocs();
  const upload = useUploadDoc();

  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [sourceKey, setSourceKey] = useState("");
  const [version, setVersion] = useState("1");
  const [effectiveFrom, setEffectiveFrom] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  // 文件选择框是**非受控**的：把 state 清成 null 并不会清掉它显示的文件名，
  // 换一个 key 让它重新挂载才算真的清空（否则用户会看到"传完了但文件名还挂着"）。
  const [fileKey, setFileKey] = useState(0);

  function resetForm() {
    setFile(null);
    setTitle("");
    setSourceKey("");
    setVersion("1");
    setEffectiveFrom("");
    setFileKey((key) => key + 1);
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setLocalError(null);

    // 这几条只是"别让人白等一次往返"：真正的判定全在后端（唯一约束、版本号、格式）
    if (!file) {
      setLocalError("先选一个文件");
      return;
    }
    if (title.trim() === "") {
      setLocalError("标题不能为空");
      return;
    }
    if (sourceKey.trim() === "") {
      setLocalError("source_key 不能为空 —— 它是这份制度的稳定标识（换标题不算换制度，换它才算）");
      return;
    }
    if (!/^\d+$/.test(version.trim()) || Number(version.trim()) < 1) {
      setLocalError("版本号要是 ≥ 1 的整数");
      return;
    }

    let effective: string | null = null;
    if (effectiveFrom !== "") {
      const parsed = new Date(effectiveFrom);
      if (Number.isNaN(parsed.getTime())) {
        setLocalError(`生效时间无法解析：${effectiveFrom}`);
        return;
      }
      // `datetime-local` 给的字符串**不带时区**，`new Date()` 按本机时区解释它；
      // 转成带 Z 的 ISO 再发，服务端就不用猜（与附件/供应商那边的写法一致）。
      effective = parsed.toISOString();
    }

    upload.mutate(
      {
        file,
        title: title.trim(),
        source_key: sourceKey.trim(),
        version: Number(version.trim()),
        effective_from: effective,
      },
      { onSuccess: resetForm },
    );
  }

  return (
    <>
      <PageHeader
        title="知识库"
        description="上传采购制度（带版本与生效时间）：助手只依据这里的片段回答，所以切分结果要能看见、失败原因要说清。"
        breadcrumb={[{ label: "首页", to: "/" }, { label: "管理员" }, { label: "知识库" }]}
      />

      <Card
        title="上传制度文档"
        description="上传后会做三件事：提取文本 → 切分成片段（带重叠）→ 算向量入库。同一份制度的新版本请用同一个 source_key、把版本号加一。"
      >
        <form onSubmit={submit} className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="文件" hint="支持哪些格式由后端判定，被拒时它会说明原因">
              <Input
                key={fileKey}
                data-testid="knowledge-file"
                type="file"
                onChange={(event) => setFile(event.target.files?.[0] ?? null)}
              />
            </Field>
            <Field label="标题" hint="给人看的显示名；改标题不算换制度">
              <Input
                data-testid="knowledge-title"
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                required
                maxLength={200}
                placeholder="例如：采购管理办法"
              />
            </Field>
            <Field
              label="source_key"
              hint="稳定标识：改标题不算换制度，改它才算换了一份；同一 source_key + 版本号只能有一份"
            >
              <Input
                data-testid="knowledge-source-key"
                className="font-mono"
                value={sourceKey}
                onChange={(event) => setSourceKey(event.target.value)}
                required
                maxLength={120}
                placeholder="PROCUREMENT-POLICY"
              />
            </Field>
            <Field
              label="版本号"
              hint="同一份制度的第几版；检索只取「当前生效的最高版本」，旧版不会一起被引出来"
            >
              <Input
                data-testid="knowledge-version"
                type="number"
                min={1}
                step={1}
                value={version}
                onChange={(event) => setVersion(event.target.value)}
                required
              />
            </Field>
            <Field label="生效时间" hint="留空 = 立即生效（服务端取当前时间）">
              <Input
                data-testid="knowledge-effective-from"
                type="datetime-local"
                value={effectiveFrom}
                onChange={(event) => setEffectiveFrom(event.target.value)}
              />
            </Field>
            <div className="flex items-end">
              <Button
                type="submit"
                data-testid="knowledge-submit"
                disabled={upload.isPending}
                className="w-full"
              >
                {upload.isPending ? "上传并处理中…" : "上传"}
              </Button>
            </div>
          </div>

          {localError && <p className="text-sm text-danger">{localError}</p>}
          <ErrorNote error={upload.error} />

          {upload.isSuccess && (
            <HintNote>
              上传成功，正在切分与算向量 —— 列表里那份文档的「处理中」变成「已就绪」才算真的可用
              （此页面会自动重查）。
            </HintNote>
          )}
        </form>
      </Card>

      <Card
        title="制度文档"
        description="点一行右边的「看切分」可以展开它的片段：分片号 + 字符区间 + 原文，用来判断「是没检索到」还是「切碎了」。"
      >
        {docs.isPending && <Loading label="读取文档…" />}
        <ErrorNote error={docs.error} />

        {docs.data && docs.data.length === 0 && (
          <EmptyState>
            知识库是空的 —— 上面传一份制度，助手才有东西可引。空库时提问只会得到
            「制度中没有找到相关规定」（而且不会调用模型）。
          </EmptyState>
        )}

        {docs.data && docs.data.length > 0 && (
          <div data-testid="knowledge-list">
            <Table head={DOC_COLUMNS}>
              {docs.data.map((doc) => (
                <Fragment key={doc.id}>
                  <Row className={expanded === doc.id ? "bg-section/60 align-top" : "align-top"}>
                    <Cell>
                      <span className="text-ink">{doc.title}</span>
                      {doc.filename && (
                        <span className="mt-0.5 block font-mono text-xs text-faint">
                          {doc.filename}
                        </span>
                      )}
                    </Cell>
                    <Cell>
                      <span className="font-mono text-xs text-muted">{doc.source_key}</span>
                    </Cell>
                    <Cell>第 {doc.version} 版</Cell>
                    <Cell className="text-muted">
                      {new Date(doc.effective_from).toLocaleString()}
                    </Cell>
                    <Cell>
                      <StatusCell doc={doc} />
                    </Cell>
                    <Cell>
                      <span className="font-mono">{doc.chunk_count}</span>
                      <span className="ml-1 text-xs text-faint">
                        （正文 {doc.content_chars} 字）
                      </span>
                    </Cell>
                    <Cell>
                      <span className="font-mono text-xs text-muted">{doc.embedding_model}</span>
                    </Cell>
                    <Cell>
                      {/* 展开做成这一格里的按钮而不是"点整行"：`Row` / `Cell` 不转发 `onClick`
                          （它们只管排版），硬点整行就得绕开原语自己写 `<tr>`。 */}
                      <Button
                        size="sm"
                        variant="ghost"
                        data-testid="knowledge-toggle"
                        onClick={() => setExpanded(expanded === doc.id ? null : doc.id)}
                      >
                        {expanded === doc.id ? "收起" : "看切分"}
                      </Button>
                    </Cell>
                  </Row>

                  {expanded === doc.id && (
                    <Row>
                      {/* `Cell` 不接受 `colSpan`，而这一格要横跨整行 —— 所以用原生 `<td>`；
                          颜色仍然只走语义类（`bg-section`），不写色值。 */}
                      <td colSpan={DOC_COLUMNS.length} className="bg-section/40 px-3 py-3">
                        <ChunkPanel doc={doc} />
                      </td>
                    </Row>
                  )}
                </Fragment>
              ))}
            </Table>
          </div>
        )}

        <p className="mt-3 text-xs text-faint">
          检索只取每份 <span className="font-mono">source_key</span> 里「已生效 + 版本最高」的那一版：
          所以同一份制度传第 2 版时，第 1 版留在库里（可追溯），但不会被引用。
        </p>
      </Card>
    </>
  );
}

// ------------------------------------------------------------------ 行内小件

/** 状态格子：徽标 + **失败原因 / 未就绪的说明**。
 *
 * 失败原因必须显示在列表里（不是藏在详情页）：它是用户唯一能自救的线索 ——
 * 只说"失败"等于让人去猜是格式不对、扫描件没文字层，还是向量服务没起来。
 */
function StatusCell({ doc }: { doc: KnowledgeDoc }) {
  const style = DOC_STATUS_STYLE[doc.status] ?? { label: doc.status, tone: "neutral" as const };
  return (
    <>
      <Badge tone={style.tone}>{style.label}</Badge>
      {doc.status === "FAILED" && (
        <p className="mt-1 max-w-xs whitespace-pre-wrap text-xs text-danger">
          {doc.failure_reason ?? "服务端没有给出失败原因"}
        </p>
      )}
      {doc.status === "PENDING" && (
        <p className="mt-1 max-w-xs text-xs text-muted">
          切分与向量化还没完成 —— 这期间它检索不到。
        </p>
      )}
      {doc.status === "READY" && doc.chunk_count === 0 && (
        <p className="mt-1 max-w-xs text-xs text-warning">
          一片都没切出来 —— 检索永远命中不到它。
        </p>
      )}
    </>
  );
}

/** 切分结果。**这是"没检索到还是切碎了"的判据所在**，所以三样都要给：
 *  分片号（哪一片）、字符区间（在原文的哪一段）、原文（到底切成了什么）。 */
function ChunkPanel({ doc }: { doc: KnowledgeDoc }) {
  const chunks = useKnowledgeChunks(doc.id);
  const list = chunks.data ?? [];
  const average =
    list.length > 0
      ? Math.round(list.reduce((sum, chunk) => sum + chunk.length, 0) / list.length)
      : 0;

  return (
    <div className="space-y-2" data-testid="knowledge-chunks">
      <p className="text-xs text-muted">
        《{doc.title}》第 {doc.version} 版的切分结果
        {list.length > 0 && `：共 ${list.length} 片，平均每片 ${average} 字`}
      </p>

      {chunks.isPending && <Loading label="读取切分结果…" />}
      <ErrorNote error={chunks.error} />

      {chunks.data && list.length === 0 && (
        <EmptyState>
          这份文档一个片段都没有 —— 不是「问法不对」，是它没有可检索的内容
          （原文件可能是扫描件、或正文提取失败）。
        </EmptyState>
      )}

      {list.length > 0 && (
        <ul className="space-y-2">
          {list.map((chunk) => (
            <li
              key={chunk.chunk_no}
              className="rounded-sm border border-line bg-card px-3 py-2"
            >
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-xs">
                <span className="font-mono text-ink">第 {chunk.chunk_no} 片</span>
                <span className="font-mono text-faint">
                  字符 {chunk.start_offset}–{chunk.end_offset}
                </span>
                <span className="font-mono text-faint">{chunk.length} 字</span>
              </div>
              <p className="mt-1 whitespace-pre-wrap text-xs text-body">{chunk.content}</p>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

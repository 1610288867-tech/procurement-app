/** 制度助手（M11）：问采购制度，答案带出处；查不到就说查不到。
 *
 * ============================ 为什么手写 SSE 解析 ============================
 * 1. **`EventSource` 用不了**：它只能发 GET、不能带 JSON 请求体，也拿不到错误响应体 ——
 *    401/422 会被它悄悄变成"重连中"，用户看到的是一个转圈，而不是原因。
 *    而这条接口是 `POST /api/v1/chat/stream`，注定要自己发 `fetch`。
 * 2. **为一个页面引第三方解析库不划算**：SSE 的帧格式只做一件事 ——
 *    `event:` / `data:` 行、空行结尾。真正要小心的只有两条：
 *    "一个 chunk 里可能只有半个事件"、"一个事件可能跨 chunk"，
 *    那是 `takeFrames()` 三十行的事，而且它是个**纯函数**，比库更容易看懂。
 * 3. **`request()` 也用不了**：它会把响应体整个读成 JSON，而这里要的是流。
 *    代价是 401 续期要在 `openStream()` 里补一份最小的（那里写了为什么）。
 *
 * ============================ 为什么"未调用模型"要单独显示 ============================
 * `used_model=false` 时，正文是后端**写死**的一句「制度中没有找到相关规定。」——
 * 它不是模型的回答，而是一次**拒答**：检索没命中（或全部低于相似度阈值），所以压根没调模型。
 *
 * 这件事必须让用户看出来，因为两种情况的下一步动作完全相反：
 *   - 模型看过制度后说没有 → 制度里确实没写，该去问人；
 *   - 系统根本没调模型   → 多半是**问法**与制度用词对不上（"多少钱要招标" vs 制度里的"公开招标"），
 *                          换个词再问一次就可能命中。
 * 用同一种气泡显示这两件事，用户只会得出一个错误结论："这个助手什么都不知道"。
 * 所以拒答用 HintNote + Badge 明确标出「未调用模型：<reason>」，
 * 正文也换成另一种观感（虚线框 + 弱化字色），与模型生成的回答一眼可分。
 *
 * ============================ 四条不肯让步的约定 ============================
 * 1. **只有收到 `done` 才算答完**：连接中途断掉时正文一定是残的，必须显式报错 ——
 *    最恶劣的失败不是报错，而是"看起来答完了"。
 * 2. **`sources` 只来自 `meta` 事件**：编号 `[1]` 与文档的对照关系只有服务端知道，
 *    前端不猜、不从正文里反推。正文里出现来源列表里**没有**的编号时标成红字 ——
 *    那正是"编造出处"的样子（对应后端 `check_citation_integrity` 的第 1 条）。
 * 3. **`meta` 先于正文到达**：所以引用卡片能在正文还没出来时就渲染出来，
 *    用户先看到"它要依据哪几段"，再看到答案。
 * 4. **不假装能回放历史**：会话列表只有 id / 标题 / 时间，没有"读某个会话的消息"的接口，
 *    所以选中一个会话的含义是**继续追问**，本页不会凭空造出以前的对话。
 */

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";

import { ApiError, request } from "../api/client";
import type { ApiErrorBody } from "../api/client";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorNote,
  Field,
  HintNote,
  Input,
  Loading,
  PageHeader,
} from "../components/ui";
// SSE 切帧与安全 JSON 解析抽到 `lib/sse.ts`：它们是**纯逻辑**，
// 放在页面文件里既没法单测、又让这个文件长到近千行。
import {
  asRecords,
  parseData,
  readNumber,
  readText,
  takeFrames,
  type SseFrame,
} from "../lib/sse";

// ================================================================ 类型（与后端形状一一对应）

/** 一条检索到的制度片段。**编号从 1 开始**，正文里的 `[1]` 指的就是它。 */
type ChatSource = {
  number: number;
  doc_title: string;
  doc_version: number;
  chunk_no: number;
  content: string;
  score: number;
};

/** `meta` 事件：正文一个字都还没来时，来源与"调没调模型"就已经定了。
 *
 * `used_model=false` 时 `reason` 是**唯一的解释**（为什么没调模型），
 * 所以它必须原样显示出来，不能吞掉。
 */
type ChatMeta = {
  session_id: string;
  sources: ChatSource[];
  used_model: boolean;
  reason: string;
  /// 远端模型名（本地兜底时为 `null`）。`meta` 事件里有，回放路径里没有。
  model?: string | null;
};

/** 会话列表项。历史正文由 `GET /chat/sessions/{id}/messages` 单独取。 */
type ChatSession = { id: string; title: string; created_at: string };

/** 提问请求体。`session_id` 为 `null` = 新开一个会话。 */
type ChatRequest = { question: string; session_id: string | null };

/** 一个 Agent 步骤（`event: step`）。
 *
 * 后端在 Agent 循环里**每做一步就推一条**：`tool_call`（正在查）与
 * `tool_result`（查完了）。为什么要分开发两个事件：
 * "正在查"与"查到了"是用户等的两个不同时刻 —— 只给前者会让人以为卡住了，
 * 只给后者会让人不知道它在忙什么。
 */
type AgentStep = {
  round: number;
  kind: string;
  detail: string;
  tool: string | null;
  /** 写工具预检出的**待人工确认的提案**（M12+）。
   *
   * 后端的两段式写工具只做到"准备"：模型给出一份 `draft` 与一个
   * `confirmation_token`，**落库要人点一下**。所以这里必须带着
   * `draft`（显示要建什么）与令牌（确认时原样回传）——
   * 只给一句摘要文本的话，用户**找不到确认的地方**，
   * 而 Agent 会说"请去确认"。
   */
  confirmation?: {
    action: string | null;
    draft: { title?: string; category_id?: string; reason?: string | null } | null;
    confirmation_token: string | null;
  } | null;
};

/** SSE 帧解出来的事件。`error` 有两种来源：服务端的 `error` 事件，或半个 JSON。 */
type StreamEvent =
  | { kind: "meta"; meta: ChatMeta }
  | { kind: "delta"; text: string }
  | { kind: "step"; step: AgentStep }
  | {
      kind: "done";
      //: **`done` 里的事实**（可缺省）：`used_model` 由后端按"模型有没有真的被采用"算，
      //: 与 `meta` 的预报可能不同 —— 页面以这里为准（见 `parseFrame` 里那段说明）。
      used_model?: boolean;
      reason?: string;
    }
  | { kind: "error"; message: string };

/** 一轮问答（前端的本地记录：后端没给消息 id，因为没有读历史的接口）。 */
type Turn = {
  seq: number;
  question: string;
  answer: string;
  meta: ChatMeta | null;
  /** 只有 `done` 才算 `done`；流断了是 `error`（见文件头第 1 条） */
  status: "streaming" | "done" | "error";
  error: Error | null;
  /** Agent 执行步骤（走 Agent 时才有）。**保留**而不是只显示最后一条：
   *  用户会想知道"它到底查了哪些东西"，而那只在完整列表里看得出来。 */
  steps: AgentStep[];
};

const STREAM_PATH = "/api/v1/chat/stream";
const SESSIONS_KEY = ["chat", "sessions"] as const;

export default function Chat() {
  const client = useQueryClient();
  const sessions = useQuery({ queryKey: SESSIONS_KEY, queryFn: listSessions });

  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [streaming, setStreaming] = useState(false);

  const nextSeq = useRef(0);
  const abortRef = useRef<AbortController | null>(null);

  // 离开页面就断开这条流。不 abort 的话请求会一直挂到服务端写完：
  // 既白烧一次模型调用，回来时还会看到一条"自己长出来"的回答。
  useEffect(() => () => abortRef.current?.abort(), []);

  /** 只改这一轮 —— 用序号定位，避免把上一轮的 delta 追加到这一轮上。 */
  function patchTurn(seq: number, change: (turn: Turn) => Turn) {
    setTurns((prev) => prev.map((turn) => (turn.seq === seq ? change(turn) : turn)));
  }

  /** 提问。`override` 用于「用同一句话重问」——见下面限流分支的说明。 */
  async function ask(override?: string) {
    const question = (override ?? draft).trim();
    if (question === "" || streaming) return;

    const seq = nextSeq.current;
    nextSeq.current += 1;
    setTurns((prev) => [
      ...prev,
      { seq, question, answer: "", meta: null, status: "streaming", error: null, steps: [] },
    ]);
    setDraft("");
    setStreaming(true);

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      await streamAnswer(question, sessionId, controller.signal, {
        onMeta: (meta) => {
          patchTurn(seq, (turn) => ({ ...turn, meta }));
          // 会话是**第一次提问时**由服务端建的：把 id 记下来，下一问就是"继续追问"，
          // 同时让顶部的会话列表失效（这个新会话此刻才出现在列表里）。
          if (meta.session_id !== "" && meta.session_id !== sessionId) {
            setSessionId(meta.session_id);
            void client.invalidateQueries({ queryKey: SESSIONS_KEY });
          }
        },
        // 逐段追加就是"逐字出现"本身：**不加客户端打字机**。
        // 打字机只是把已经收到的字延迟吐出来，它会让"done 了但还在慢慢显示"变成一种新状态，
        // 而且真正的流式（服务端一段一段算）反而被盖住了。
        onDelta: (text) =>
          patchTurn(seq, (turn) => ({ ...turn, answer: turn.answer + text })),
        // Agent 步骤：**追加**到这一轮上，让用户实时看到"它在查什么"。
        // 不显示的话，整个 Agent 过程对用户就是"转圈 5 秒然后出答案"——
        // 而 5 秒的空白会让人以为卡住了、甚至刷新页面把请求丢掉。
        onStep: (step) =>
          patchTurn(seq, (turn) => ({ ...turn, steps: [...turn.steps, step] })),
        onError: (message) =>
          patchTurn(seq, (turn) => ({ ...turn, status: "error", error: new Error(message) })),
        onDone: (final) =>
          // 报过错之后又收到 done（理论上不会有）：**不能把错误擦掉** ——
          // 红字消失比错误本身更糟，用户会以为这段正文是完整的。
          patchTurn(seq, (turn) =>
            turn.status === "error"
              ? turn
              : {
                  ...turn,
                  status: "done",
                  // ⚠️ **用 `done` 的事实覆盖 `meta` 的预报**（实测踩到）：
                  // `meta` 先发、只能预报；Agent 若一个工具都没调，后端会退回本地兜底
                  // 那句写死的话术 —— 那时 `meta` 报的"用了模型"是错的，
                  // 而页面不覆盖就会**正文是拒答句、却不显示「未调用模型」**。
                  // 只在 `done` 真的给了值时覆盖（缺省时保持 `meta` 的说法）。
                  meta: turn.meta
                    ? {
                        ...turn.meta,
                        used_model: final.used_model ?? turn.meta.used_model,
                        reason: final.reason ?? turn.meta.reason,
                      }
                    : turn.meta,
                },
          ),
      });
    } catch (error) {
      const normalized = toError(error);
      // ============================ 限流（429）要特殊对待 ============================
      // 为什么不能只靠下面那行通用的 `ErrorNote`：
      //   ① **用户输入会被吃掉**。`setDraft("")` 在发请求**之前**执行（见上面），
      //      而 429 是在流开始**之前**抛出的 —— 于是问题从输入框消失、
      //      回答区只有一行红字，用户得把刚才那句话重新打一遍。
      //      这里把它**还回输入框**（只在输入框为空时还，免得盖掉用户新打的字）。
      //   ② 限流是**等一会儿就能成功**的状态，不是"出错了"。
      //      文案要说清"等多久/为什么"，并给一个「用同一句话重问」的按钮 ——
      //      否则用户只能靠反复点发送来试探，而反复点正是限流要拦的行为。
      const limited =
        normalized instanceof ApiError &&
        (normalized.status === 429 || normalized.code === "CHAT_QUOTA_EXCEEDED");
      if (limited) {
        setDraft((current) => (current === "" ? question : current));
      }
      patchTurn(seq, (turn) => ({
        ...turn,
        status: "error",
        error: normalized,
      }));
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void ask();
  }

  return (
    <>
      <PageHeader
        title="制度助手"
        description="问采购制度上的事（多少钱要招标、报销标准…）：答案带出处，查不到就说查不到。"
        breadcrumb={[{ label: "首页", to: "/" }, { label: "制度助手" }]}
      />

      <SessionCard
        query={sessions}
        sessionId={sessionId}
        onPick={setSessionId}
        // 流式回答中途换会话，回答就落在一个"已经不是当前会话"的地方了 —— 先等它答完
        disabled={streaming}
      />

      {/* 选中历史会话时把它以前的问答读回来（**只读**）。
          放在"问答"卡片之前：先看到"上次怎么说的"，再决定接着问什么 ——
          这也是用户点开旧会话时的真实动线。 */}
      {sessionId !== null && <HistoryPanel sessionId={sessionId} />}

      <Card
        title="问答"
        description="回答只依据知识库里的制度片段，并标注引用编号；检索不到时系统不调用模型，直接说「制度中没有找到相关规定」。"
      >
        {turns.length === 0 ? (
          <EmptyState>
            还没有问过 —— 在下面的输入框里问一句制度上的事，例如「多少钱的项目必须公开招标？」。
          </EmptyState>
        ) : (
          <ol className="space-y-5" data-testid="chat-messages">
            {turns.map((turn, index) => (
              <TurnView
                key={turn.seq}
                turn={turn}
                /* 只有**最后一轮**挂 `chat-answer` / `chat-sources`：
                   验收要看的是"最新的那条正在逐字长出来"，而多轮之后
                   "取第一个匹配"会拿到一条早写完了的旧回答，看起来就像流式没生效。 */
                isLatest={index === turns.length - 1}
                /* 限流后「重问这一句」——只在没在流式回答时可用 */
                onRetry={streaming ? undefined : () => void ask(turn.question)}
              />
            ))}
          </ol>
        )}

        <form className="mt-5 border-t border-line pt-4" onSubmit={submit}>
          <div className="flex flex-wrap items-end gap-3">
            <Field
              label="问题"
              className="min-w-0 flex-1"
              hint="用制度里的原词更容易检索到（例如「公开招标」而不是「招标」）"
            >
              <Input
                data-testid="chat-input"
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                placeholder="例如：多少钱的项目必须公开招标？"
                disabled={streaming}
                maxLength={500}
              />
            </Field>
            <Button
              type="submit"
              data-testid="chat-send"
              disabled={streaming || draft.trim() === ""}
            >
              {streaming ? "回答中…" : "提问"}
            </Button>
          </div>
        </form>
      </Card>
    </>
  );
}

// ------------------------------------------------------------------ 历史会话

/** 会话列表。**顺序由服务端给**（新的在前），前端不重排 —— 重排就得自己解析时间，
 *  而"哪个是最近的"是服务端更清楚的事。 */
function SessionCard({
  query,
  sessionId,
  onPick,
  disabled,
}: {
  query: { data?: ChatSession[]; isPending: boolean; isError: boolean; error: unknown };
  sessionId: string | null;
  onPick: (id: string | null) => void;
  disabled: boolean;
}) {
  return (
    <Card
      title="历史会话"
      description="选中一个会话 = 在它下面继续追问；「新会话」会另起一段。"
    >
      {query.isPending && <Loading label="读取会话…" />}
      {query.isError && <ErrorNote error={query.error} />}

      {query.data && query.data.length === 0 && (
        <EmptyState>还没有历史会话 —— 提第一个问题之后它才会出现。</EmptyState>
      )}

      {query.data && query.data.length > 0 && (
        <ul className="flex flex-wrap gap-2" data-testid="chat-sessions">
          {query.data.map((session) => (
            <li key={session.id}>
              <button
                type="button"
                data-testid="chat-session"
                disabled={disabled}
                onClick={() => onPick(session.id)}
                className={
                  "block max-w-xs rounded-md border px-3 py-1.5 text-left text-xs transition-colors disabled:opacity-50 " +
                  (session.id === sessionId
                    ? "border-primary bg-section text-ink"
                    : "border-line text-muted hover:bg-hover hover:text-ink")
                }
              >
                <span className="block truncate">{session.title}</span>
                <span className="mt-0.5 block font-mono text-faint">
                  {new Date(session.created_at).toLocaleString()}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}

      <div className="mt-3 flex flex-wrap items-center gap-3 text-xs text-muted">
        <Button
          size="sm"
          variant="ghost"
          data-testid="chat-new-session"
          disabled={disabled || sessionId === null}
          onClick={() => onPick(null)}
        >
          新会话
        </Button>
        <span>
          当前：
          {sessionId ? (
            <span className="font-mono">{sessionId.slice(0, 8)}…</span>
          ) : (
            "新会话（还没落库）"
          )}
        </span>
      </div>

      <div className="mt-3">
        <HintNote>
          选中一个历史会话会把它以前的问答**读回来**（只读展示，不影响继续追问）。
          回放不挂 `chat-answer` 那类 testid —— 那些是给"当前这一轮"用的，
          回放内容混进去会让浏览器实测取到一段早就写完的旧回答。
        </HintNote>
      </div>
    </Card>
  );
}

// ------------------------------------------------------------------ 历史回放

/** 某个会话以前的消息（只读）。
 *
 * ============================ 为什么要有它 ============================
 * 没有它的话，会话列表只能"切过去接着问"，用户点开一个旧会话看到的是一片空白 ——
 * 而"上次它到底怎么说的"正是查制度时最需要回看的。
 * 后端一直有 `GET /chat/sessions/{id}/messages`。
 *
 * ============================ 两条不肯让步的地方 ============================
 * 1. **只读**：回放里不提供任何"重新提问/重试"的按钮。那一轮已经发生过了，
 *    给它加上操作入口会让"历史"变成"可以改的东西"。
 * 2. **不挂当前轮的 testid**：回放内容全部走 `-history` 或独立 testid。
 *    混用会让浏览器实测"取第一个匹配"时拿到旧回答 —— 看起来像流式没生效。
 */
function HistoryPanel({ sessionId }: { sessionId: string }) {
  const query = useQuery({
    queryKey: ["chat", "messages", sessionId],
    queryFn: () => listMessages(sessionId),
  });

  if (query.isPending) {
    return (
      <Card title="历史回放" description="读回这个会话以前的问答…">
        <Loading label="读取历史消息…" />
      </Card>
    );
  }
  if (query.isError) {
    return (
      <Card title="历史回放">
        <ErrorNote error={query.error} />
      </Card>
    );
  }

  // 成对取出（USER 提问 + ASSISTANT 回答）。后端按 `seq` 正序返回，
  // 所以这里只做一次线性配对，不排序 —— 排序会把"服务端的顺序"这件事藏起来，
  // 而顺序错了本身就是该被发现的问题。
  const pairs: { question: string; answer: string | null; meta: ChatMeta | null }[] = [];
  for (const message of query.data ?? []) {
    if (message.role === "USER") {
      pairs.push({ question: message.content, answer: null, meta: null });
    } else {
      const current = pairs[pairs.length - 1];
      // 没有对应的提问就不配对（数据异常时宁可少显示一轮，也不要自己编一个提问出来）
      if (!current) continue;
      current.answer = message.content;
      current.meta = {
        session_id: sessionId,
        sources: (message.sources ?? []) as ChatSource[],
        used_model: message.used_model,
        // 历史消息没有"当时为什么这么答"的完整原话，只有**引用校验的问题**是存下来的。
        // 有它就显示出来（那是"这条回答的出处不可信"的痕迹），
        // 没有就说"历史消息" —— **不编一句看起来像当时的理由**。
        reason: message.citation_problems?.length
          ? `来源校验未通过：${message.citation_problems.join("；")}`
          : "（历史消息：当时未留下「为什么这样答」的原文）",
        model: null,
      };
    }
  }

  return (
    <Card
      title="历史回放"
      description={`这个会话有 ${pairs.length} 轮问答（只读）。`}
      id="section-history"
    >
      {pairs.length === 0 ? (
        <EmptyState>这个会话还没有问答。</EmptyState>
      ) : (
        <ul className="space-y-4" data-testid="chat-history">
          {pairs.map((pair, index) => (
            <li key={index} className="space-y-2">
              <div className="rounded-md bg-section px-3 py-2">
                <p className="text-xs text-muted">我问</p>
                <p className="mt-0.5 whitespace-pre-wrap text-sm text-ink">
                  {pair.question}
                </p>
              </div>
              <div className="rounded-md border border-line bg-card px-3 py-2">
                <p className="text-xs text-muted">助手</p>
                {pair.answer === null ? (
                  <p className="mt-1 text-sm text-warning">
                    这一轮没有回答（当时可能失败了，或回答还在生成中）。
                  </p>
                ) : (
                  <>
                    <div className="mt-1 break-words text-sm text-body">
                      <AnswerBody
                        text={pair.answer}
                        sources={pair.meta?.sources ?? []}
                        testId="chat-history-answer"
                      />
                    </div>
                    {(pair.meta?.sources.length ?? 0) > 0 && (
                      <SourceList
                        sources={pair.meta?.sources ?? []}
                        testId="chat-history-sources"
                      />
                    )}
                  </>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

// ------------------------------------------------------------------ 一轮问答
/** 这一轮的失败是不是"被限流"（而不是真的出错）。
 *
 * 限流与其他错误要分开处理：它是**等一会儿再试就会成功**的状态，
 * 文案与操作都不同（见 `RateLimitNote`）；混在通用红字里，
 * 用户只会反复点发送 —— 而反复点恰恰是限流要拦的行为。
 *
 * 判据同时看状态码与业务码：只看状态码的话，
 * 将来别处也用 429 就会被误判成问答限流。
 */
function isRateLimited(error: Error | null): boolean {
  return (
    error instanceof ApiError &&
    (error.status === 429 || error.code === "CHAT_QUOTA_EXCEEDED")
  );
}

function TurnView({
  turn,
  isLatest,
  onRetry,
}: {
  turn: Turn;
  isLatest: boolean;
  /** 「用这句话重问」——不传就不显示那个按钮（例如正在流式回答时） */
  onRetry?: () => void;
}) {
  const meta = turn.meta;
  // 未调用模型 = 一次**拒答**，与"模型看过制度说没有"不是一回事（见文件头）
  const refused = meta !== null && !meta.used_model;
  const sources = meta?.sources ?? [];
  const limited = isRateLimited(turn.error);

  return (
    <li className="space-y-2" data-testid="chat-turn">
      <div className="rounded-md bg-section px-3 py-2">
        <p className="text-xs text-muted">我问</p>
        <p className="mt-0.5 whitespace-pre-wrap text-sm text-ink">{turn.question}</p>
      </div>

      <div
        className={
          "rounded-md border px-3 py-2 " +
          (refused
            ? "border-dashed border-line-strong bg-section/40"
            : "border-line bg-card")
        }
      >
        <div className="flex flex-wrap items-center gap-2">
          <p className="text-xs text-muted">助手</p>
          {refused && <Badge tone="warning">未调用模型</Badge>}
          {turn.status === "error" && <Badge tone="danger">未完成</Badge>}
          {turn.status === "streaming" && <span className="text-xs text-faint">正在回答…</span>}
        </div>

        {/* Agent 执行步骤。放在正文**之前**：它就是"回答出来之前发生了什么"，
            顺序上先看到它才符合真实过程。
            ⚠️ 只在走 Agent 时才有内容 —— 制度问答那条路不经过工具，步骤为空。 */}
        {turn.steps.length > 0 && (
          <AgentSteps steps={turn.steps} running={turn.status === "streaming"} />
        )}

        {refused && (
          <div className="mt-2">
            <HintNote>
              未调用模型：{meta?.reason || "服务端没有给出原因"}。
              这段正文是系统写死的「制度中没有找到相关规定」，
              <strong>不是模型的回答</strong> —— 换成制度里的原词再问一次，通常就能检索到。
            </HintNote>
          </div>
        )}

        <div className={"mt-2 break-words text-sm " + (refused ? "text-muted" : "text-body")}>
          {turn.status === "streaming" && turn.answer === "" && (
            <p className="mb-1 text-xs text-faint">正在检索制度…</p>
          )}
          <AnswerBody
            text={turn.answer}
            sources={sources}
            testId={isLatest ? "chat-answer" : "chat-answer-history"}
          />
          {/* 光标放在正文**外面**：正文那一格只放答案本身，
              否则取值时会多出一个 ▍，看起来像答案里混进了符号 */}
          {turn.status === "streaming" && turn.answer !== "" && (
            <span aria-hidden="true" className="ml-0.5 animate-pulse text-primary">
              ▍
            </span>
          )}
        </div>

        {sources.length > 0 && (
          <SourceList
            sources={sources}
            testId={isLatest ? "chat-sources" : "chat-sources-history"}
          />
        )}

        {!refused && turn.status === "done" && sources.length === 0 && (
          <p className="mt-2 text-xs text-warning">
            这条回答没有任何来源 —— 正常情况下它不该出现，请把这句话连同问题一起反馈。
          </p>
        )}

        {limited ? (
          <RateLimitNote message={turn.error?.message} onRetry={onRetry} />
        ) : (
          turn.error && <ErrorNote error={turn.error} />
        )}
      </div>
    </li>
  );
}

/** 被限流时的提示（**不是**普通错误）。
 *
 * ============================ 为什么单独一个块，而不复用 `ErrorNote` ============================
 * 三件事不一样：
 *   1. **它是"等一会儿就能成功"**，不是"出错了"。所以要给出"为什么"和"怎么办"，
 *      而不是把后端那句话原样一贴（原样贴出来用户也不知道下一步做什么）。
 *   2. **要给一个重问入口**。没有它，用户只能反复点发送来试探 ——
 *      而反复点正是限流要拦的行为（也是个坏循环：越试越被拦）。
 *   3. **要用 warning 而不是 danger 的色**。限流是系统的保护机制在正常工作，
 *      不是故障；用错误色会让用户以为功能坏了（实测：被限流的人第一反应是"这功能有 bug"）。
 *
 * `message` 里已经带了后端算好的"等多久 / 用超了多少"，所以原样显示它 ——
 * 前端不复述那个数字（复述就会有两处真相，改一处忘一处）。
 */
function RateLimitNote({
  message,
  onRetry,
}: {
  message?: string;
  onRetry?: () => void;
}) {
  return (
    <div
      className="mt-3 rounded-sm border border-warning/40 bg-warning-bg px-3 py-2 text-sm text-warning"
      data-testid="chat-rate-limit"
    >
      <p className="font-medium">提问太频繁，这条问题没有发出去</p>
      <p className="mt-1">
        {message ?? "服务端做了提问频率限制。"}
      </p>
      <p className="mt-1">
        你刚才那句话已经<strong>放回上面的输入框</strong>，稍等片刻可以直接再发一次。
      </p>
      {onRetry && (
        <div className="mt-2">
          <Button type="button" variant="ghost" onClick={onRetry}>
            现在就用这句话重问
          </Button>
        </div>
      )}
    </div>
  );
}

/** 工具名 → 给用户看的中文标签。
 *
 * 为什么不直接把工具名显示出来：`list_my_purchase_requests` 是**给模型看的标识**，
 * 显示给用户就是泄漏内部命名。而这个映射**故意不放进后端**——
 * 它是纯展示，改文案不该要求重发一次接口。
 *
 * 没有映射的工具**原样显示工具名**（而不是"未知工具"）：
 * 新加工具时忘了加映射的表现应该是"看到了一个英文名"，
 * 而不是"看到未知工具"（后者会让人以为出了故障）。
 */
const TOOL_LABELS: Record<string, string> = {
  list_my_purchase_requests: "查询采购申请",
  get_purchase_request: "查看申请详情",
  list_my_approval_todos: "查询我的待办",
  create_purchase_draft: "准备创建申请草稿",
};

/** Agent 执行步骤列表。
 *
 * ============================ 为什么要显示它 ============================
 * 一次 Agent 提问要几秒到几十秒（模型回合 + 工具查询）。**这段时间里界面上
 * 什么都不动**，用户的第一反应是"卡住了"，然后刷新页面 —— 而刷新会把请求丢掉。
 * 把"正在查什么 / 查到了什么"显示出来，等待就变成了可理解的过程。
 *
 * 这也是 Agent 与普通问答在**体感上**最大的差别：普通问答是"打字机"，
 * Agent 是"它去办事了"。不显示步骤的话，后者看起来就像前者的卡顿版。
 */
function AgentSteps({ steps, running }: { steps: AgentStep[]; running: boolean }) {
  return (
    <ol className="mt-2 space-y-1" data-testid="chat-agent-steps">
      {steps.map((step, index) => {
        const isLast = index === steps.length - 1;
        // 三种 `kind` 各给一句话，**互斥**（用 map 而不是嵌套三元：
        // 嵌套三元在这里要写四层，而四层三元没人读得懂）。
        const text =
          step.kind === "tool_call"
            ? `正在${TOOL_LABELS[step.tool ?? ""] ?? step.tool ?? "查询"}…`
            : step.detail;
        // `tool_call` 还没回来时转圈，其余都是已完成的勾。
        // `guard`（护栏触发：超时/轮次用尽）用 warning 色，它需要用户注意。
        const isGuard = step.kind === "guard";
        const pending = step.kind === "tool_call" && isLast && running;
        const confirmation = step.confirmation;
        return (
          <li
            key={`${step.round}-${index}`}
            className="flex items-start gap-2 text-xs"
            data-testid={isGuard ? "chat-agent-guard" : "chat-agent-step"}
          >
            <span aria-hidden="true" className="mt-0.5 text-faint">
              {pending ? "◐" : "✓"}
            </span>
            <span className={isGuard ? "text-warning" : "text-muted"}>{text}</span>
            {/* 写工具是两段式：模型只能**准备**，落库必须人点这一下。
                所以确认入口必须在这里出现 —— 不给它的话，
                Agent 会说"请去确认"而用户在界面上找不到任何可确认的地方
                （这正是这条链原先的缺口）。 */}
            {confirmation?.confirmation_token && confirmation.draft ? (
              <DraftConfirmCard
                draft={confirmation.draft}
                token={confirmation.confirmation_token}
              />
            ) : null}
          </li>
        );
      })}
    </ol>
  );
}

/** "确认创建"卡片：把 Agent 准备的草稿显示出来，等人点确认。
 *
 * ============================ 为什么必须显示"要建什么" ============================
 * 确认令牌是**可以推算**的（`uuid5(actor+标题+品类+备注)`），所以后端在确认时
 * 会**逐字重算令牌比对** —— 它确认的是"这一份提案"，不是某个字符串。
 * 界面上把 `draft` 显示出来，是与那条纪律配对的一半：
 * 用户看到的必须**就是**他确认的那份内容。
 *
 * 确认后只提示结果，**不把草稿塞进会话**：真正建出来的那张单在"我的申请"里，
 * 而这里再造一份前端状态，就会有两个事实来源。
 */
function DraftConfirmCard({
  draft,
  token,
}: {
  draft: NonNullable<NonNullable<AgentStep["confirmation"]>["draft"]>;
  token: string;
}) {
  const [state, setState] = useState<"idle" | "sending" | "done" | "replayed" | "error">("idle");
  const [message, setMessage] = useState<string>("");

  async function confirm() {
    setState("sending");
    try {
      const result = await request<{
        request_id: string;
        code: string;
        title: string;
        status: string;
        replayed: boolean;
      }>("/api/v1/chat/confirm-draft", {
        method: "POST",
        body: JSON.stringify({
          confirmation_token: token,
          // **原样回传**：后端会逐字重算令牌比对，改一个字段就会 404
          draft,
        }),
      });
      setState(result.replayed ? "replayed" : "done");
      setMessage(
        result.replayed
          ? `这张单已经建过了：${result.code}（重复确认不会建第二张）`
          : `已创建：${result.code} · ${result.title} · 状态 ${result.status}`,
      );
    } catch (error) {
      setState("error");
      setMessage(toError(error).message);
    }
  }

  return (
    <div
      className="mt-1 w-full rounded border border-line bg-surface p-2"
      data-testid="chat-draft-confirm"
    >
      <div className="text-xs text-muted">
        待确认的草稿（<span className="font-mono">还没有创建</span>）
      </div>
      <div className="mt-1 text-sm">
        <div>标题：{draft.title ?? "（未提供）"}</div>
        {draft.reason ? <div className="text-muted">说明：{draft.reason}</div> : null}
      </div>
      <div className="mt-2 flex items-center gap-2">
        <Button
          size="sm"
          data-testid="chat-draft-confirm-button"
          disabled={state === "sending" || state === "done" || state === "replayed"}
          onClick={confirm}
        >
          {state === "sending" ? "创建中…" : "确认创建"}
        </Button>
        {message ? (
          <span
            data-testid="chat-draft-confirm-result"
            className={state === "error" ? "text-xs text-danger" : "text-xs text-muted"}
          >
            {message}
          </span>
        ) : null}
      </div>
    </div>
  );
}

/** 正文 + 引用编号。
 *
 * `[1]` 用正则切开渲染成小标记：**能对上来源列表的**用主色并带上出处提示，
 * **对不上的**标红 —— 后者是模型编造出处的典型样子，宁可显眼也不要让它混在正文里。
 */
function AnswerBody({
  text,
  sources,
  testId,
}: {
  text: string;
  sources: ChatSource[];
  testId: string;
}) {
  const known = new Map(sources.map((source) => [source.number, source]));
  const parts: ReactNode[] = [];
  // 每次渲染新建正则：带 `g` 的正则是有状态的（`lastIndex`），复用会漏掉匹配
  const pattern = /\[(\d+)\]/g;
  let cursor = 0;

  for (const match of text.matchAll(pattern)) {
    const at = match.index ?? 0;
    const digits = match[1] ?? "";
    if (at > cursor) parts.push(text.slice(cursor, at));
    const source = known.get(Number(digits));
    parts.push(
      <span
        key={`${at}-${digits}`}
        data-testid={source ? "chat-citation" : "chat-citation-unknown"}
        className={"font-mono " + (source ? "text-primary" : "text-danger")}
        title={
          source
            ? `《${source.doc_title}》第 ${source.doc_version} 版 第 ${source.chunk_no} 段`
            : "来源列表里没有这个编号 —— 可能是模型编造的出处"
        }
      >
        [{digits}]
      </span>,
    );
    cursor = at + (match[0]?.length ?? 0);
  }
  if (cursor < text.length) parts.push(text.slice(cursor));

  return (
    <p data-testid={testId} className="inline whitespace-pre-wrap">
      {parts}
    </p>
  );
}

/** 引用卡片：编号 / 文档标题 / 第几版 / 第几段 / 相似度 + 原文片段。
 *
 * 原文片段要显示出来：用户判断"这条能不能支撑上面那句话"靠的是读原文，
 * 只给一个标题等于让他去信一个看不见的东西。
 */
function SourceList({ sources, testId }: { sources: ChatSource[]; testId: string }) {
  return (
    <div className="mt-3 space-y-2" data-testid={testId}>
      <p className="text-xs text-muted">来源（正文里的 [n] 对应下面的编号）</p>
      <ul className="space-y-2">
        {sources.map((source) => (
          <li
            key={source.number}
            className="rounded-sm border border-line bg-section px-3 py-2"
          >
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-xs">
              <span className="font-mono text-ink">[{source.number}]</span>
              <span className="text-ink">{source.doc_title}</span>
              <span className="text-muted">第 {source.doc_version} 版</span>
              <span className="text-muted">第 {source.chunk_no} 段</span>
              <span className="font-mono text-faint">
                相似度 {source.score.toFixed(2)}
              </span>
            </div>
            <p className="mt-1 line-clamp-4 whitespace-pre-wrap text-xs text-body">
              {source.content}
            </p>
          </li>
        ))}
      </ul>
      <p className="text-xs text-faint">
        引用卡片来自服务端的 <span className="font-mono">meta</span> 事件
        （正文还没开始时就已经到了），不是从答案里反推的。
      </p>
    </div>
  );
}

// ------------------------------------------------------------------ 传输层

/** 会话列表。`request()` 那条路会处理 401 → 续期 → 重放，这里直接用它。 */
function listSessions(): Promise<ChatSession[]> {
  return request<ChatSession[]>("/api/v1/chat/sessions");
}

/** 某个会话的历史消息（**只读回放**：`GET /chat/sessions/{id}/messages`）。
 *
 * `sources` 与 `citation_problems` 是后端在**回答当时**存下的快照，
 * 所以回放看到的是"当时引用了什么"，不会因为制度文档后来改版而变 ——
 * 这正是审计想要的形态。
 */
type ChatMessage = {
  id: string;
  role: "USER" | "ASSISTANT";
  content: string;
  seq: number;
  sources: ChatSource[] | null;
  citation_problems: string[] | null;
  used_model: boolean;
  top_score: number | null;
  created_at: string;
};

function listMessages(sessionId: string): Promise<ChatMessage[]> {
  return request<ChatMessage[]>(`/api/v1/chat/sessions/${sessionId}/messages`);
}

type StreamHandlers = {
  onMeta: (meta: ChatMeta) => void;
  onDelta: (text: string) => void;
  /** Agent 执行步骤（只有走 Agent 时才会有）。**可选**：
   *  不想显示步骤的调用方（例如只关心正文的回放路径）不必实现它。 */
  onStep?: (step: AgentStep) => void;
  /** 收尾。参数是 `done` 事件里的**事实**（可能缺省）：
   *  `used_model` 由后端按"模型有没有真的被采用"算，与 `meta` 的预报可能不同 ——
   *  调用方应当用它覆盖 `meta.used_model`，否则徽标会与正文矛盾。 */
  onDone: (final: { used_model?: boolean; reason?: string }) => void;
  onError: (message: string) => void;
};

/** 发一次流式提问，返回**还没读**的 Response。
 *
 * ============================ 为什么这里要自己补一份 401 续期 ============================
 * `request()` 里的"401 → 续期 → 重放"没法复用：它会把响应体整个读成 JSON，
 * 而这里要的是**流**。access 令牌只有 15 分钟，用户问着问着就会撞上 401，
 * 所以在这里补一次最小实现：续期成功就原样重放一次，失败就把 401 如实抛出去。
 * **只重放一次** —— 与 `client.ts` 的约定一致（续期失败再问下去就是死循环）。
 */
async function openStream(payload: ChatRequest, signal: AbortSignal): Promise<Response> {
  const init: RequestInit = {
    method: "POST",
    // 同源时浏览器本来就会带 Cookie，写出来是为了与 `client.ts` 的约定一致
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(payload),
    signal,
  };

  let response = await fetch(STREAM_PATH, init);
  if (response.status === 401 && (await refreshAccessToken())) {
    response = await fetch(STREAM_PATH, init);
  }

  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as ApiErrorBody;
    // 后端不可达时，Vite 代理返回的是**空响应体的 500**，而不是连接错误 ——
    // 不特判的话用户只会看到"提问失败：500"，完全看不出是后端没启动（与 `client.ts` 同源）。
    const unreachable = !body.error && [500, 502, 503, 504].includes(response.status);
    throw new ApiError(
      response.status === 401
        ? "登录状态已过期：刷新页面重新登录后再提问。"
        : (body.error?.message ??
          (unreachable
            ? `提问失败：${response.status} —— 后端可能没启动（先确认 uvicorn 在 8000 端口跑着）`
            : `提问失败：${response.status}`)),
      response.status,
      body.error?.code,
      body.error?.request_id,
    );
  }
  return response;
}

/** 换一张新的 access 令牌。与 `client.ts` 里那份同源（原因见 `openStream`）：
 *  失败一律返回 `false`，让调用方看到原本那个 401。 */
async function refreshAccessToken(): Promise<boolean> {
  try {
    const response = await fetch("/api/v1/auth/refresh", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
    });
    return response.ok;
  } catch {
    return false;
  }
}

/** 读一条流，沿途把事件交给调用方。
 *
 * ============================ 三个必须处理对的边界 ============================
 * 1. **一个 chunk 里可能只有半个事件**：所以要有缓冲区，切不出来的部分留到下一轮
 *    （`takeFrames` 返回的 `rest`）。
 * 2. **一个事件可能跨 chunk**：同上，靠同一个缓冲区解决，不能按 chunk 直接解。
 * 3. **流可能在没收到 `done` 时结束**：那就是断线，正文一定是残的 —— 抛错，
 *    **不把半截回答当成完整答案**。
 */
async function streamAnswer(
  question: string,
  sessionId: string | null,
  signal: AbortSignal,
  handlers: StreamHandlers,
): Promise<void> {
  const response = await openStream({ question, session_id: sessionId }, signal);
  const body = response.body;
  if (!body) {
    // 200 但没有响应体：没有流可读，绝不能当成"回答是空的"
    throw new ApiError("服务端没有返回响应流（response.body 为空）", response.status);
  }

  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  /** 服务端明确说这一轮结束了（`done`）或者已经报过错。**没收尾就是断了。** */
  let closed = false;

  const emit = (frame: SseFrame) => {
    const event = parseFrame(frame);
    if (!event) return;
    if (event.kind === "meta") handlers.onMeta(event.meta);
    else if (event.kind === "delta") handlers.onDelta(event.text);
    else if (event.kind === "step") handlers.onStep?.(event.step);
    else if (event.kind === "done") {
      // ⚠️ **`done` 不只是"可以收尾了"，它必须回调出去**。
      // 这里原先只置了 `closed = true` 而没调 `handlers.onDone()` ——
      // 后果是页面永远停在"正在回答…"（`turn.status` 永远到不了 `done`），
      // 光标一直闪，而回答其实早就写完了。
      // 实测由浏览器实测脚本发现（它被迫改用"提问按钮文案"判断答完没答完）。
      closed = true;
      // 把 `done` 里的**事实**（`used_model` / `reason`）一起交出去：
      // 页面用它覆盖 `meta` 的预报，徽标才不会与正文矛盾（见 `parseFrame` 的说明）。
      handlers.onDone({ used_model: event.used_model, reason: event.reason });
    } else {
      closed = true;
      handlers.onError(event.message);
    }
  };

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const cut = takeFrames(buffer);
    buffer = cut.rest;
    for (const frame of cut.frames) emit(frame);
  }

  // 收尾做两件事：① 解码器里可能还压着半个多字节字符（`stream: true` 的代价，
  // 不补这一下会把最后一个汉字丢掉）；② 最后一个事件后面**可能没有空行**，
  // 补一个分隔符才切得出来。
  buffer += decoder.decode();
  for (const frame of takeFrames(`${buffer}\n\n`).frames) emit(frame);

  if (!closed) {
    throw new ApiError(
      "连接在回答结束前断开了：上面这段正文不完整。请重新提问。",
      0,
    );
  }
}

/** 把任意抛出物收敛成 Error（`ErrorNote` 只认 `.message`）。 */
function toError(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error));
}

// ------------------------------------------------------------------ 帧 → 页面事件

function readSource(record: Record<string, unknown>): ChatSource {
  return {
    number: readNumber(record.number),
    doc_title: readText(record.doc_title),
    doc_version: readNumber(record.doc_version),
    chunk_no: readNumber(record.chunk_no),
    content: readText(record.content),
    score: readNumber(record.score),
  };
}

/** 帧里的 `confirmation`（写工具预检出的待确认提案）→ 有类型的对象。
 *
 * **缺字段一律返回 `null`**（而不是补一个空壳）：确认按钮要拿着
 * `draft` + `confirmation_token` 原样回传，少一个就建不成单 ——
 * 那种情况下**宁可不显示按钮**，也不要给一个点了必然 404 的按钮。
 * 这与后端 `verify_confirmation` 的"逐字比对"是同一条纪律的两端。
 */
function readConfirmation(value: unknown): AgentStep["confirmation"] {
  if (typeof value !== "object" || value === null) return null;
  const record = value as Record<string, unknown>;
  const draft = record.draft;
  const token = record.confirmation_token;
  if (typeof token !== "string" || token.length === 0) return null;
  if (typeof draft !== "object" || draft === null) return null;
  const d = draft as Record<string, unknown>;
  return {
    action: typeof record.action === "string" ? record.action : null,
    draft: {
      title: typeof d.title === "string" ? d.title : undefined,
      category_id: typeof d.category_id === "string" ? d.category_id : undefined,
      reason: typeof d.reason === "string" ? d.reason : null,
    },
    confirmation_token: token,
  };
}

/** 帧 → 有类型的事件。未知事件名返回 `null`（丢弃而不是报错）：
 *  后端将来加事件（心跳、用量…）时前端不该崩，而正文只走 `delta` 一条路，
 *  所以"忽略一个不认识的事件"与"丢掉一段正文"是两件事。
 *
 * 切帧与 JSON 解析在 `lib/sse.ts`（那部分是通用逻辑、有单测）；
 * **这里只负责业务字段的解读** —— 那些字段属于本页面的领域模型。 */
function parseFrame(frame: SseFrame): StreamEvent | null {
  const parsed = parseData(frame);
  if (!parsed.ok) {
    // 解析失败**不静默跳过**：跳过等于把缺失的那段正文当成没发生过。
    return { kind: "error", message: parsed.message };
  }
  const record = parsed.value;

  if (frame.event === "meta") {
    return {
      kind: "meta",
      meta: {
        session_id: readText(record.session_id),
        sources: asRecords(record.sources).map(readSource),
        used_model: record.used_model === true,
        reason: readText(record.reason),
      },
    };
  }
  if (frame.event === "delta") return { kind: "delta", text: readText(record.text) };
  if (frame.event === "step") {
    // Agent 执行步骤。字段名与后端 `_answer_with_agent` 推的一致：
    // `round` / `kind`（tool_call | tool_result | guard）/ `detail` / `tool`
    // / `confirmation`（写工具预检出的待确认提案）。
    return {
      kind: "step",
      step: {
        round: readNumber(record.round),
        kind: readText(record.kind),
        detail: readText(record.detail),
        tool: typeof record.tool === "string" ? record.tool : null,
        confirmation: readConfirmation(record.confirmation),
      },
    };
  }
  if (frame.event === "done") {
    // ⚠️ **`done` 里带的是"事实"，`meta` 里的是"预报"**（实测踩到）：
    // `meta` 必须在正文之前发出去，所以它只能预报"这次会不会用模型"；
    // 而 Agent 路径下模型可能**一个工具都不调**，那时后端会退回本地兜底那句写死的话术 ——
    // 采用的字**不是模型生成的**，可 `meta` 已经报了"用了模型"，页面于是不显示徽标，
    // 而正文却是写死的拒答句。`done.used_model` 由后端按**事实**算，这里带出来覆盖预报。
    return {
      kind: "done",
      used_model: typeof record.used_model === "boolean" ? record.used_model : undefined,
      reason: readText(record.reason) || undefined,
    };
  }
  if (frame.event === "error") {
    return {
      kind: "error",
      message: readText(record.message) || "服务端返回了未知错误",
    };
  }
  return null;
}

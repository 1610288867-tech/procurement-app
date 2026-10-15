/**
 * SSE 切帧与**安全** JSON 解析（纯函数，可单测）。
 *
 * ============================ 为什么它单独一个模块 ============================
 * 做两件事，都只依赖输入、不依赖 React 状态：
 *   `takeFrames`  从字节流缓冲区里切出完整的帧（处理"半个事件"）
 *   `parseData`   把一帧的 `data` 安全地解成对象 / 报出一条错误
 *
 * 放在页面文件里时它们**没法被单测**（不是导出），而它们恰恰最该被测：
 * 切帧错了的表现是"事件一个都不触发、正文一个字都不出"，
 * JSON 解析错了的表现是"某个字段永远是默认值" —— **两者都不报错**。
 *
 * ⚠️ **本模块不认识业务字段**：它只回答"有哪些帧""这一帧的 data 解得开吗"。
 * 把 `meta`/`delta`/`step` 的字段解读留在页面里，是因为那些字段属于页面的领域模型；
 * 放进通用模块会让它变成"什么都懂一点"的杂货铺，而那种模块没人敢改。
 */

/** 一个 SSE 帧：`event: delta` + `data: {...}` 这样的若干行。`data` 还没解成 JSON。 */
export type SseFrame = { event: string; data: string };

/** `data` 的解析结果。**解析失败也是一等结果**（不是异常）。 */
export type ParsedData =
  | { ok: true; value: Record<string, unknown> }
  | { ok: false; message: string };

/** 从缓冲区里切出**完整的**帧，剩下的半个留在 `rest` 里等下一个 chunk。
 *
 * 只认三件事（SSE 规范里与我们有关系的部分）：
 *   - 行前缀 `event:` / `data:`，字段名后允许一个空格；
 *   - 空行 = 一个帧结束；
 *   - `:` 开头的行是注释（心跳），丢掉。
 * 字段值里的冒号属于值（例如 `data: {"a":"b:c"}`），所以只按**第一个**冒号切。
 */
export function takeFrames(buffer: string): { frames: SseFrame[]; rest: string } {
  // 换行先统一：SSE 允许 \r\n / \r / \n 三种。不统一的话 "\r\n\r\n" 这个分隔符会被漏掉，
  // 表现是"事件一个都不触发、正文一个字都不出"。
  const blocks = buffer.replace(/\r\n?/g, "\n").split("\n\n");
  // 最后一段是**没写完的**：它必须留到下一个 chunk 拼上再解析
  const rest = blocks.pop() ?? "";

  const frames: SseFrame[] = [];
  for (const block of blocks) {
    let name = "";
    const dataLines: string[] = [];
    for (const line of block.split("\n")) {
      if (line === "" || line.startsWith(":")) continue;
      const colon = line.indexOf(":");
      const field = colon === -1 ? line : line.slice(0, colon);
      const raw = colon === -1 ? "" : line.slice(colon + 1);
      const value = raw.startsWith(" ") ? raw.slice(1) : raw;
      if (field === "event") name = value;
      else if (field === "data") dataLines.push(value);
    }
    // 多行 `data:` 按规范用换行拼起来；没有 data 的帧（例如纯心跳）直接丢
    if (dataLines.length > 0) {
      frames.push({ event: name === "" ? "message" : name, data: dataLines.join("\n") });
    }
  }
  return { frames, rest };
}

/** 把一帧的 `data` 解成对象。
 *
 * **解析失败返回 `{ok:false}` 而不是抛异常**，理由与页面里那条注释一致：
 * 半个 JSON 只可能来自"连接断在事件中间"，这时**不能静默跳过** ——
 * 跳过等于把缺失的那段正文当成没发生过。所以调用方一定会拿到一个可显示的说明。
 *
 * 顶层不是对象（例如 `data: 123`）也算失败：本协议所有事件都是对象，
 * 而"解出一个数字然后到处读字段"只会得到一堆 undefined。
 */
export function parseData(frame: SseFrame): ParsedData {
  let payload: unknown;
  try {
    payload = JSON.parse(frame.data) as unknown;
  } catch {
    return { ok: false, message: `服务端事件无法解析（event: ${frame.event}）—— 回答可能不完整` };
  }
  if (typeof payload !== "object" || payload === null || Array.isArray(payload)) {
    return { ok: false, message: `服务端事件不是对象（event: ${frame.event}）` };
  }
  return { ok: true, value: payload as Record<string, unknown> };
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/** `Array.isArray` 会把类型收成 `any[]`，这里自己收一次，顺带过滤掉非对象项。 */
export function asRecords(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.filter(isRecord) : [];
}

export function readText(value: unknown): string {
  return typeof value === "string" ? value : "";
}

export function readNumber(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

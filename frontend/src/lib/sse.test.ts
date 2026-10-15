/**
 * SSE 切帧与解析的纯函数测试。
 *
 * ============================ 为什么这两件事必须被测试 ============================
 * 它们错了**都不会报错**：
 *   · 切帧漏掉 `\r\n\r\n` 分隔符 → "事件一个都不触发、正文一个字都不出"；
 *   · 半个事件没留到下一轮 → 正文少一段；
 *   · JSON 解析失败被静默跳过 → 缺失的那段当成没发生过。
 *
 * 所以每一条边界都要有断言。它们是纯函数，测起来是毫秒级。
 */

import { describe, expect, it } from "vitest";

import {
  asRecords,
  parseData,
  readNumber,
  readText,
  takeFrames,
  type SseFrame,
} from "./sse";

/** 取第一帧（**断言它存在**）：
/** 取第一帧（断言它存在）。
 *
 * `noUncheckedIndexedAccess` 下 `frames[0]` 可空，到处写 `!` 会让断言散落一地 ——
 * 那种"到处是感叹号"的测试里，真正的断言反而看不见了。
 */
function first(frames: SseFrame[]): SseFrame {
  const head = frames[0];
  if (!head) throw new Error(`期望至少一帧，实际 ${frames.length} 帧`);
  return head;
}

function frame(event: string, data: unknown): SseFrame {
  return { event, data: JSON.stringify(data) };
}

describe("takeFrames", () => {
  it("切出完整的帧，并把没写完的留在 rest", () => {
    const buffer = "event: delta\ndata: {\"text\":\"甲\"}\n\nevent: delta\ndata: {\"text\":\"乙\"}";
    const { frames, rest } = takeFrames(buffer);
    expect(frames).toHaveLength(1);
    expect(JSON.parse(first(frames).data)).toEqual({ text: "甲" });
    // 第二帧没有空行结尾 → 必须留在 rest 里等下一个 chunk
    expect(rest).toContain("乙");
  });

  it("接受 \\r\\n 行尾（不统一换行会一个事件都切不出来）", () => {
    const { frames } = takeFrames("event: delta\r\ndata: {\"text\":\"甲\"}\r\n\r\n");
    expect(frames).toHaveLength(1);
    expect(first(frames).event).toBe("delta");
  });

  it("丢掉注释行（心跳）", () => {
    const { frames } = takeFrames(": keep-alive\n\nevent: delta\ndata: {\"text\":\"甲\"}\n\n");
    expect(frames).toHaveLength(1);
    expect(first(frames).event).toBe("delta");
  });

  it("没有 data 的帧直接丢（纯心跳不该产生事件）", () => {
    const { frames } = takeFrames("event: ping\n\n");
    expect(frames).toHaveLength(0);
  });

  it("多行 data 按换行拼起来", () => {
    const { frames } = takeFrames("event: delta\ndata: 第一行\ndata: 第二行\n\n");
    expect(first(frames).data).toBe("第一行\n第二行");
  });

  it("字段值里的冒号属于值（只按第一个冒号切）", () => {
    const { frames } = takeFrames('event: delta\ndata: {"a":"b:c"}\n\n');
    expect(JSON.parse(first(frames).data)).toEqual({ a: "b:c" });
  });

  it("缺 event 名的帧按 message 处理（规范如此）", () => {
    const { frames } = takeFrames("data: {}\n\n");
    expect(first(frames).event).toBe("message");
  });

  it("跨两个 chunk 的事件能拼回来", () => {
    const chunk1 = takeFrames('event: delta\ndata: {"text":"甲');
    expect(chunk1.frames).toHaveLength(0);
    const chunk2 = takeFrames(`${chunk1.rest}"}\n\n`);
    expect(chunk2.frames).toHaveLength(1);
    expect(JSON.parse(first(chunk2.frames).data)).toEqual({ text: "甲" });
  });
});

describe("parseData", () => {
  it("正常 JSON 对象解出来", () => {
    expect(parseData(frame("delta", { text: "甲" }))).toEqual({
      ok: true,
      value: { text: "甲" },
    });
  });

  it("**坏 JSON 返回 ok:false 而不是抛异常**（要能变成一条给用户看的错误）", () => {
    const result = parseData({ event: "delta", data: '{"text":"甲' });
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.message).toContain("delta");
      expect(result.message).toContain("不完整");
    }
  });

  it("顶层不是对象也算失败（解出数字/数组只会到处读到 undefined）", () => {
    expect(parseData({ event: "delta", data: "123" }).ok).toBe(false);
    expect(parseData({ event: "delta", data: "[1,2]" }).ok).toBe(false);
    expect(parseData({ event: "delta", data: "null" }).ok).toBe(false);
  });
});

describe("字段读取的兜底", () => {
  it("readText 对非字符串返回空串", () => {
    expect(readText("甲")).toBe("甲");
    expect(readText(123)).toBe("");
    expect(readText(null)).toBe("");
    expect(readText(undefined)).toBe("");
  });

  it("readNumber 对非有限数字返回 0", () => {
    expect(readNumber(3)).toBe(3);
    expect(readNumber(0)).toBe(0);
    expect(readNumber("3")).toBe(0);
    expect(readNumber(NaN)).toBe(0);
    expect(readNumber(Infinity)).toBe(0);
  });

  it("asRecords 过滤掉非对象项", () => {
    expect(asRecords([{ a: 1 }, 2, null, "x", { b: 2 }])).toEqual([{ a: 1 }, { b: 2 }]);
    expect(asRecords("不是数组")).toEqual([]);
  });
});

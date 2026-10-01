/** 金额转换的单元测试。
 *
 * 这是 M0 唯一的"真"测试，选它的理由：**转换写错 1000 倍是这类系统最经典的
 * 线上事故**，而它又是纯函数，测起来几乎零成本。
 *
 * 运行：cd frontend && npx vitest run
 */

import { describe, expect, it } from "vitest";

import { fenToYuan, formatFen, yuanToFen } from "./money";

describe("yuanToFen", () => {
  it("整数元", () => {
    expect(yuanToFen("1000")).toBe(100_000);
    expect(yuanToFen("0")).toBe(0);
  });

  it("带小数", () => {
    expect(yuanToFen("12.3")).toBe(1230);
    expect(yuanToFen("12.34")).toBe(1234);
  });

  it("容忍常见输入噪声（千分位、空格、货币符号）", () => {
    expect(yuanToFen("1,000")).toBe(100_000);
    expect(yuanToFen(" ¥ 12,345.67 ")).toBe(1_234_567);
  });

  it("非法输入抛错，而不是静默返回 0", () => {
    // 静默把错误金额存进库里，比直接报错危险得多
    expect(() => yuanToFen("")).toThrow();
    expect(() => yuanToFen("abc")).toThrow();
    expect(() => yuanToFen("1.234")).toThrow(); // 超过两位小数
    expect(() => yuanToFen("-5")).toThrow();
  });
});

describe("fenToYuan", () => {
  it("补足两位小数", () => {
    expect(fenToYuan(100_000)).toBe("1000.00");
    expect(fenToYuan(1230)).toBe("12.30");
    expect(fenToYuan(5)).toBe("0.05");
  });

  it("拒绝非整数分（说明有人不小心用了浮点）", () => {
    expect(() => fenToYuan(12.5)).toThrow();
  });
});

describe("formatFen", () => {
  it("带千分位与货币符号", () => {
    expect(formatFen(1_234_567)).toBe("¥12,345.67");
    expect(formatFen(0)).toBe("¥0.00");
  });
});

describe("往返一致", () => {
  it("分 → 元 → 分 不丢精度", () => {
    for (const fen of [0, 1, 99, 100, 1234, 100_000, 20_000_000, 999_999_999]) {
      expect(yuanToFen(fenToYuan(fen))).toBe(fen);
    }
  });
});

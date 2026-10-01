/** 金额单位转换的唯一入口。
 *
 * ============================ 为什么单独一个文件 ============================
 * 界面显示「元」，传输和存储全程「分」。这个转换写错 1000 倍是这类系统最经典的
 * 线上事故，所以全项目**只有这一个文件**允许碰金额单位。
 *
 * 纪律：
 *   - 后端和数据库全程只认「分」（BIGINT / int），禁止 float
 *   - 前端只在渲染和提交两个边界处转换
 *   - 转换一律用整数运算，不要引入浮点
 */

/** 「元」文本 → 「分」整数。接受 "1000"、"1000.5"、"1,000"。
 *
 * 非法输入抛错而不是返回 0 —— 静默把错误金额存进库里，比报错危险得多。
 */
export function yuanToFen(input: string): number {
  const normalized = input.replace(/[,\s¥￥]/g, "");
  if (normalized === "" || !/^\d+(\.\d{1,2})?$/.test(normalized)) {
    throw new Error(`金额格式不合法：${input}（最多两位小数）`);
  }
  const [yuan, fraction = ""] = normalized.split(".");
  const fen = fraction.padEnd(2, "0");
  return Number(yuan) * 100 + Number(fen);
}

/** 「分」整数 → 「元」文本，保留两位小数。 */
export function fenToYuan(fen: number): string {
  if (!Number.isInteger(fen)) {
    throw new Error(`金额必须是整数分：${fen}`);
  }
  const sign = fen < 0 ? "-" : "";
  const abs = Math.abs(fen);
  return `${sign}${Math.floor(abs / 100)}.${String(abs % 100).padStart(2, "0")}`;
}

/** 「分」整数 → 带千分位的展示文本，如 "¥12,000.00"。 */
export function formatFen(fen: number): string {
  const [yuan, decimals] = fenToYuan(fen).split(".");
  const sign = yuan?.startsWith("-") ? "-" : "";
  const digits = (yuan ?? "").replace("-", "");
  const grouped = digits.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${sign}¥${grouped}.${decimals}`;
}

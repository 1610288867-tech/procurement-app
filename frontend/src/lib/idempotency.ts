/** 幂等键的生成（M5）。
 *
 * 服务端要求 `Idempotency-Key`：同一个键重复提交**只会执行一次**，
 * 之后每次重放都返回第一次的响应。键由客户端生成，所以"什么时候换一把新的"
 * 是决定性的一件事：
 *
 *   - 一次用户动作（点一次"提交"）= 一把新键；
 *   - **同一个动作的重试必须复用同一把键** —— 网络超时后重试，服务端才认得出
 *     "这是同一件事"。每次请求都新生成一把，就等于没有幂等：重试会变成第二次提交。
 *
 * 所以键在**发起动作之前**生成一次，成功之后才丢弃；用 `lib/idempotency.ts`
 * 的 `newIdempotencyKey()` 产生。
 */

export function newIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

/** HTTP 客户端的静默续期测试。
 *
 * access 令牌只有 15 分钟。过期时**不该把用户踢回登录页** ——
 * 正确做法是静默换一张令牌、把这次请求原样重放一遍，用户感知不到。
 *
 * 这里直接替换掉全局 fetch，测的是真实行为（"第几次调用返回了什么"），
 * 不是把被测代码 mock 掉。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, api } from "./client";

const REFRESH_PATH = "/api/v1/auth/refresh";

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const UNAUTHORIZED = { error: { code: "UNAUTHORIZED", message: "未登录或登录已过期" } };

let fetchMock: ReturnType<typeof vi.fn>;

function calledPaths(): string[] {
  return fetchMock.mock.calls.map((call) => String(call[0]));
}

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("过期后的静默续期", () => {
  it("收到 401 时先换令牌，再把原请求重放一次", async () => {
    fetchMock
      .mockResolvedValueOnce(json(401, UNAUTHORIZED)) // 原请求：令牌过期
      .mockResolvedValueOnce(json(200, { id: "u1" })) // 续期成功
      .mockResolvedValueOnce(json(200, [{ id: "s1" }])); // 重放

    const suppliers = await api.listSuppliers();

    expect(suppliers).toEqual([{ id: "s1" }]);
    expect(calledPaths()).toEqual([
      "/api/v1/suppliers",
      REFRESH_PATH,
      "/api/v1/suppliers",
    ]);
  });

  it("续期失败时把原来的 401 抛出去，不无限重放", async () => {
    fetchMock
      .mockResolvedValueOnce(json(401, UNAUTHORIZED))
      .mockResolvedValueOnce(json(401, UNAUTHORIZED)); // 续期也失败

    await expect(api.listSuppliers()).rejects.toBeInstanceOf(ApiError);
    // 只有两次：原请求 + 一次续期。没有第三次重放
    expect(calledPaths()).toEqual(["/api/v1/suppliers", REFRESH_PATH]);
  });

  it("重放之后仍然 401 就认输，不会再来一轮", async () => {
    fetchMock
      .mockResolvedValueOnce(json(401, UNAUTHORIZED))
      .mockResolvedValueOnce(json(200, { id: "u1" }))
      .mockResolvedValueOnce(json(401, UNAUTHORIZED)); // 重放还是 401

    await expect(api.listSuppliers()).rejects.toBeInstanceOf(ApiError);
    expect(calledPaths()).toEqual([
      "/api/v1/suppliers",
      REFRESH_PATH,
      "/api/v1/suppliers",
    ]);
  });

  it("并发的多个 401 只触发一次续期（单飞）", async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url === REFRESH_PATH) return json(200, { id: "u1" });
      // 第一次打某路径返回 401，重放时返回 200
      const seen = calledPaths().filter((p) => p === url).length;
      return seen === 1 ? json(401, UNAUTHORIZED) : json(200, { ok: true });
    });

    await Promise.all([api.listSuppliers(), api.listDepartments(), api.listCategories()]);

    const refreshCalls = calledPaths().filter((path) => path === REFRESH_PATH);
    expect(refreshCalls).toHaveLength(1);
  });

  it("登录接口自己返回 401 时不去续期（那是密码错，不是令牌过期）", async () => {
    fetchMock.mockResolvedValueOnce(
      json(401, { error: { code: "INVALID_CREDENTIALS", message: "用户名或密码不正确" } }),
    );

    await expect(api.login({ username: "a", password: "b" })).rejects.toBeInstanceOf(ApiError);
    expect(calledPaths()).toEqual(["/api/v1/auth/login"]);
  });

  it("确实没登录时（两个 Cookie 都没有）续期也失败，只多一次调用就认输", async () => {
    fetchMock
      .mockResolvedValueOnce(json(401, UNAUTHORIZED)) // /me
      .mockResolvedValueOnce(json(401, UNAUTHORIZED)); // 续期也失败

    await expect(api.me()).rejects.toBeInstanceOf(ApiError);
    expect(calledPaths()).toEqual(["/api/v1/auth/me", REFRESH_PATH]);
  });
});

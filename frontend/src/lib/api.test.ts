import { describe, expect, it, vi } from "vitest";

import { ApiClient, ApiError, readCookie } from "./api";

const json = (status: number, body: unknown) =>
  new Response(body === undefined ? null : JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("ApiClient", () => {
  it("sends the CSRF cookie on unsafe requests only", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(json(200, { ok: true }));
    const api = new ApiClient(fetchImpl, (n) => (n === "ofmp_csrf" ? "tok123" : ""));
    await api.request("GET", "/api/v1/x");
    await api.request("POST", "/api/v1/x", { a: 1 });
    expect(fetchImpl.mock.calls[0][1].headers["X-CSRF-Token"]).toBeUndefined();
    expect(fetchImpl.mock.calls[1][1].headers["X-CSRF-Token"]).toBe("tok123");
    expect(fetchImpl.mock.calls[1][1].credentials).toBe("same-origin");
  });

  it("refreshes once on 401 and retries", async () => {
    const fetchImpl = vi.fn()
      .mockResolvedValueOnce(json(401, { error: { code: "UNAUTHENTICATED", message: "expired" } }))
      .mockResolvedValueOnce(json(200, {}))                     // /refresh
      .mockResolvedValueOnce(json(200, { email: "a@b.c" }));    // retry
    const api = new ApiClient(fetchImpl, () => "t");
    await expect(api.me()).resolves.toEqual({ email: "a@b.c" });
    expect(fetchImpl.mock.calls.map((c) => c[0])).toEqual(["/api/v1/auth/me", "/api/v1/auth/refresh", "/api/v1/auth/me"]);
  });

  it("shares one refresh between concurrent 401s", async () => {
    const fetchImpl = vi.fn((path: string) =>
      Promise.resolve(path.endsWith("/refresh") ? json(200, {}) : json(401, { error: { code: "X", message: "m" } })));
    const api = new ApiClient(fetchImpl as unknown as typeof fetch, () => "t");
    await Promise.allSettled([api.me(), api.me(), api.me()]);
    expect(fetchImpl.mock.calls.filter((c) => c[0].endsWith("/refresh"))).toHaveLength(1);
  });

  it("surfaces the error envelope and does not refresh on a failed login", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(
      json(401, { error: { code: "INVALID_CREDENTIALS", message: "email or password is incorrect", request_id: "r1" } }));
    const api = new ApiClient(fetchImpl, () => "t");
    const err = await api.login("a@b.c", "x").catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect([err.status, err.code, err.message, err.requestId]).toEqual([401, "INVALID_CREDENTIALS", "email or password is incorrect", "r1"]);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("does not attempt a refresh without a session (no CSRF cookie)", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(json(401, { error: { code: "UNAUTHENTICATED", message: "x" } }));
    const api = new ApiClient(fetchImpl, () => "");
    await expect(api.me()).rejects.toBeInstanceOf(ApiError);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it("reads cookies by exact name", () => {
    expect(readCookie("ofmp_csrf", "a=1; ofmp_csrf=x%20y; ofmp_csrf2=no")).toBe("x y");
    expect(readCookie("missing", "a=1")).toBe("");
  });
});

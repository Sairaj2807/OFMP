// /api/v1 client. Session tokens live in HttpOnly cookies the browser sends
// automatically; this code never sees them. Unsafe requests echo the
// ofmp_csrf cookie in X-CSRF-Token (double-submit). An expired access token
// is refreshed once, transparently, then the request is retried.
import type { Contract, ReplaySessionInfo, User, Workspace, WorkspaceSummary } from "./types";

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
    public readonly requestId?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export function readCookie(name: string, cookieString: string = typeof document !== "undefined" ? document.cookie : ""): string {
  const match = cookieString.match(new RegExp(`(?:^|;\\s*)${name}=([^;]*)`));
  return match ? decodeURIComponent(match[1]) : "";
}

type Fetch = typeof fetch;

export class ApiClient {
  private refreshing: Promise<boolean> | null = null;

  constructor(
    private readonly fetchImpl: Fetch = (...args) => fetch(...args),
    private readonly getCookie: (name: string) => string = (n) => readCookie(n),
  ) {}

  private async raw(method: string, path: string, body?: unknown, keepalive = false): Promise<Response> {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (method !== "GET") headers["X-CSRF-Token"] = this.getCookie("ofmp_csrf");
    return this.fetchImpl(path, {
      method,
      headers,
      credentials: "same-origin",
      body: body === undefined ? undefined : JSON.stringify(body),
      keepalive, // lets a save started while the page unloads complete
    });
  }

  /** One refresh at a time, shared by every request that hit a 401 meanwhile. */
  refresh(): Promise<boolean> {
    // No CSRF cookie means no session was ever established in this browser:
    // nothing to refresh (and the server would reject the attempt).
    if (!this.getCookie("ofmp_csrf")) return Promise.resolve(false);
    this.refreshing ??= this.raw("POST", "/api/v1/auth/refresh")
      .then((r) => r.ok)
      .catch(() => false)
      .finally(() => {
        this.refreshing = null;
      });
    return this.refreshing;
  }

  async request<T>(method: string, path: string, body?: unknown, keepalive = false): Promise<T> {
    let res = await this.raw(method, path, body, keepalive);
    const isAuthCall = path.startsWith("/api/v1/auth/login") || path.startsWith("/api/v1/auth/refresh");
    if (res.status === 401 && !isAuthCall && (await this.refresh())) {
      res = await this.raw(method, path, body, keepalive);
    }
    if (res.status === 204) return undefined as T;
    const data = await res.json().catch(() => null);
    if (!res.ok) {
      const err = data?.error;
      throw new ApiError(res.status, err?.code ?? "HTTP_ERROR", err?.message ?? `request failed (${res.status})`,
        err?.request_id);
    }
    return data as T;
  }

  me = () => this.request<User>("GET", "/api/v1/auth/me");
  login = (email: string, password: string) =>
    this.request<{ user: User; expires_in: number }>("POST", "/api/v1/auth/login", { email, password });
  logout = () => this.request<void>("POST", "/api/v1/auth/logout");
  contracts = () => this.request<{ data: Contract[] }>("GET", "/api/v1/market/contracts");
  replaySessions = () => this.request<{ data: ReplaySessionInfo[] }>("GET", "/api/v1/market/replay/sessions");

  workspaces = () => this.request<WorkspaceSummary[]>("GET", "/api/v1/workspaces");
  workspace = (id: string) => this.request<Workspace>("GET", `/api/v1/workspaces/${encodeURIComponent(id)}`);
  createWorkspace = (name: string, config: object, configVersion: number) =>
    this.request<Workspace>("POST", "/api/v1/workspaces", { name, config, config_version: configVersion });
  updateWorkspace = (id: string, revision: number,
                     patch: { name?: string; config?: object; config_version?: number; is_default?: boolean },
                     keepalive = false) =>
    this.request<Workspace>("PATCH", `/api/v1/workspaces/${encodeURIComponent(id)}`, { revision, ...patch }, keepalive);
  duplicateWorkspace = (id: string, name: string) =>
    this.request<Workspace>("POST", `/api/v1/workspaces/${encodeURIComponent(id)}/duplicate`, { name });
  deleteWorkspace = (id: string) => this.request<void>("DELETE", `/api/v1/workspaces/${encodeURIComponent(id)}`);

  marketStatus = () =>
    this.request<{ contract: Contract | null; feed: { connected: boolean; error?: string | null } | null }>(
      "GET", "/api/v1/market/status");
}

export const api = new ApiClient();

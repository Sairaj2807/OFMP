// /ws/v1/stream client. Framework-free so it can be unit tested with a fake
// WebSocket. One connection carries one subscription per chart (by id).
//
// - Reconnects with exponential backoff + full jitter; resets after a welcome.
// - Re-sends every subscription after reconnecting.
// - Answers server pings; tracks seq and counts gaps (none are expected).
// - Close 4401 (session expired/revoked): asks onUnauthenticated() once
//   (e.g. refresh the session) and reconnects only if that succeeded.
// - Close 4403 (forbidden origin/permission): stops; retrying cannot help.
import type { ChartSettings, ChartSnapshot, ServerMessage } from "./types";

export type StreamStatus = "idle" | "connecting" | "open" | "reconnecting" | "unauthenticated" | "forbidden" | "stopped";

interface SocketLike {
  readyState: number;
  send(data: string): void;
  close(code?: number): void;
  onopen: ((ev: unknown) => void) | null;
  onclose: ((ev: { code: number }) => void) | null;
  onmessage: ((ev: { data: string }) => void) | null;
  onerror: ((ev: unknown) => void) | null;
}

export interface StreamOptions {
  url: () => string;
  onSnapshot: (id: string, snapshot: ChartSnapshot) => void;
  onWelcome?: (data: { intervals?: number[]; heartbeat_sec?: number }) => void;
  onStatus?: (status: StreamStatus, detail?: string) => void;
  onServerError?: (code: string, message: string, id?: string) => void;
  onUnauthenticated?: () => Promise<boolean>;
  createSocket?: (url: string) => SocketLike;
  schedule?: (fn: () => void, ms: number) => unknown;
  cancel?: (handle: unknown) => void;
  random?: () => number;
  baseDelayMs?: number;
  maxDelayMs?: number;
}

const OPEN = 1;

export class StreamClient {
  private socket: SocketLike | null = null;
  private readonly subscriptions = new Map<string, ChartSettings & { contract?: string }>();
  private attempt = 0;
  private timer: unknown = null;
  private running = false;
  private lastSeq = 0;
  status: StreamStatus = "idle";
  seqGaps = 0;

  constructor(private readonly o: StreamOptions) {}

  start(): void {
    if (this.running) return;
    this.running = true;
    this.connect();
  }

  stop(): void {
    this.running = false;
    if (this.timer !== null) (this.o.cancel ?? clearTimeout)(this.timer as ReturnType<typeof setTimeout>);
    this.timer = null;
    this.socket?.close(1000);
    this.socket = null;
    this.setStatus("stopped");
  }

  subscribe(id: string, settings: ChartSettings, contract?: string): void {
    this.subscriptions.set(id, { ...settings, contract });
    this.sendSubscribe(id);
  }

  unsubscribe(id: string): void {
    if (this.subscriptions.delete(id)) this.send({ action: "unsubscribe", id });
  }

  private setStatus(status: StreamStatus, detail?: string): void {
    this.status = status;
    this.o.onStatus?.(status, detail);
  }

  private send(msg: object): void {
    if (this.socket && this.socket.readyState === OPEN) this.socket.send(JSON.stringify(msg));
  }

  private sendSubscribe(id: string): void {
    const s = this.subscriptions.get(id);
    if (!s) return;
    const msg: Record<string, unknown> = { action: "subscribe", id, streams: ["chart"], ppr: s.ppr, interval: s.interval };
    if (s.contract) msg.contract = s.contract;
    this.send(msg);
  }

  private connect(): void {
    this.timer = null;
    this.setStatus(this.attempt === 0 ? "connecting" : "reconnecting");
    const socket = (this.o.createSocket ?? ((u) => new WebSocket(u) as unknown as SocketLike))(this.o.url());
    this.socket = socket;
    this.lastSeq = 0;
    socket.onmessage = (ev) => this.onMessage(ev.data);
    socket.onerror = () => socket.close();
    socket.onclose = (ev) => this.onClose(socket, ev.code);
  }

  private onMessage(raw: string): void {
    let msg: ServerMessage;
    try {
      msg = JSON.parse(raw);
    } catch {
      return;
    }
    if (this.lastSeq && msg.seq !== this.lastSeq + 1) this.seqGaps += 1;
    this.lastSeq = msg.seq;
    switch (msg.type) {
      case "welcome":
        this.attempt = 0;
        this.o.onWelcome?.((msg.data ?? {}) as { intervals?: number[] });
        this.setStatus("open");
        for (const id of this.subscriptions.keys()) this.sendSubscribe(id);
        break;
      case "ping":
        this.send({ action: "pong" });
        break;
      case "snapshot":
        if (msg.id && this.subscriptions.has(msg.id)) this.o.onSnapshot(msg.id, msg.data as ChartSnapshot);
        break;
      case "error": {
        const d = (msg.data ?? {}) as { code?: string; message?: string };
        this.o.onServerError?.(d.code ?? "ERROR", d.message ?? "", msg.id);
        break;
      }
    }
  }

  private async onClose(socket: SocketLike, code: number): Promise<void> {
    if (socket !== this.socket) return; // an old socket closing after a reconnect
    this.socket = null;
    if (!this.running) return;
    if (code === 4403) {
      this.running = false;
      this.setStatus("forbidden");
      return;
    }
    if (code === 4401) {
      this.setStatus("unauthenticated");
      const recovered = this.o.onUnauthenticated ? await this.o.onUnauthenticated() : false;
      if (!recovered || !this.running) {
        this.running = false;
        return;
      }
    }
    this.scheduleReconnect();
  }

  nextDelay(): number {
    const base = this.o.baseDelayMs ?? 500;
    const max = this.o.maxDelayMs ?? 15_000;
    const cap = Math.min(max, base * 2 ** this.attempt);
    this.attempt += 1;
    return Math.round((this.o.random ?? Math.random)() * cap);
  }

  private scheduleReconnect(): void {
    this.setStatus("reconnecting");
    this.timer = (this.o.schedule ?? setTimeout)(() => this.connect(), this.nextDelay());
  }
}

export function defaultStreamUrl(): string {
  const override = process.env.NEXT_PUBLIC_STREAM_URL;
  if (override) return override;
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}/ws/v1/stream`;
}

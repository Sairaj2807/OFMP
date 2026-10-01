import { describe, expect, it, vi } from "vitest";

import { StreamClient, type StreamStatus } from "./stream";

class FakeSocket {
  static all: FakeSocket[] = [];
  readyState = 0;
  sent: Record<string, unknown>[] = [];
  onopen: ((e: unknown) => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onerror: ((e: unknown) => void) | null = null;
  seq = 0;
  constructor(public url: string) {
    FakeSocket.all.push(this);
  }
  send(data: string) {
    this.sent.push(JSON.parse(data));
  }
  close(code = 1006) {
    this.readyState = 3;
    this.onclose?.({ code });
  }
  // server side helpers
  open() {
    this.readyState = 1;
    this.emit({ type: "welcome", data: { intervals: [60, 300] } });
  }
  emit(msg: Record<string, unknown>) {
    this.seq += 1;
    this.onmessage?.({ data: JSON.stringify({ seq: this.seq, ts: 0, ...msg }) });
  }
}

function setup(extra: Partial<ConstructorParameters<typeof StreamClient>[0]> = {}) {
  FakeSocket.all = [];
  const timers: (() => void)[] = [];
  const statuses: StreamStatus[] = [];
  const snapshots: [string, unknown][] = [];
  const client = new StreamClient({
    url: () => "ws://test/ws/v1/stream",
    createSocket: (u) => new FakeSocket(u),
    schedule: (fn) => timers.push(fn),
    random: () => 1,
    onSnapshot: (id, s) => snapshots.push([id, s]),
    onStatus: (s) => statuses.push(s),
    ...extra,
  });
  return { client, timers, statuses, snapshots, socket: () => FakeSocket.all.at(-1)! };
}

describe("StreamClient", () => {
  it("subscribes after the welcome and routes snapshots by id", () => {
    const { client, socket, snapshots, statuses } = setup();
    client.subscribe("c1", { ppr: 1, interval: 60 });
    client.start();
    expect(socket().sent).toEqual([]);                 // not open yet: nothing sent
    socket().open();
    expect(statuses).toEqual(["connecting", "open"]);
    expect(socket().sent).toEqual([{ action: "subscribe", id: "c1", streams: ["chart"], ppr: 1, interval: 60 }]);
    socket().emit({ type: "snapshot", id: "c1", data: { ready: true } });
    socket().emit({ type: "snapshot", id: "zz", data: { ready: true } });   // unknown id ignored
    expect(snapshots).toEqual([["c1", { ready: true }]]);
  });

  it("answers pings and counts sequence gaps", () => {
    const { client, socket } = setup();
    client.start();
    socket().open();
    socket().emit({ type: "ping" });
    expect(socket().sent.at(-1)).toEqual({ action: "pong" });
    socket().seq += 3;                                     // server skipped messages
    socket().emit({ type: "ping" });
    expect(client.seqGaps).toBe(1);
  });

  it("reconnects with growing backoff and resubscribes everything", () => {
    const { client, socket, timers } = setup();
    client.subscribe("c1", { ppr: 1, interval: 60 });
    client.subscribe("c2", { ppr: 2, interval: 300 });
    client.start();
    socket().open();
    socket().close(1006);
    expect(client.status).toBe("reconnecting");
    expect(timers).toHaveLength(1);
    expect(client.nextDelay()).toBe(1000);                // attempt 1 already used 500
    timers.shift()!();
    socket().open();
    expect(socket().sent.map((m) => m.id)).toEqual(["c1", "c2"]);
    expect(client.status).toBe("open");
  });

  it("refreshes the session once on 4401 and gives up if that fails", async () => {
    const refresh = vi.fn().mockResolvedValueOnce(true).mockResolvedValueOnce(false);
    const { client, socket, timers } = setup({ onUnauthenticated: refresh });
    client.start();
    socket().open();
    socket().close(4401);
    await Promise.resolve();
    await Promise.resolve();
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(timers).toHaveLength(1);                       // refreshed: reconnect scheduled
    timers.shift()!();
    socket().close(4401);
    await Promise.resolve();
    await Promise.resolve();
    expect(timers).toHaveLength(0);                       // refresh failed: stays down
    expect(client.status).toBe("unauthenticated");
  });

  it("stops for good when forbidden", () => {
    const { client, socket, timers } = setup();
    client.start();
    socket().close(4403);
    expect(client.status).toBe("forbidden");
    expect(timers).toHaveLength(0);
  });

  it("unsubscribe tells the server and stops routing", () => {
    const { client, socket, snapshots } = setup();
    client.subscribe("c1", { ppr: 1, interval: 60 });
    client.start();
    socket().open();
    client.unsubscribe("c1");
    expect(socket().sent.at(-1)).toEqual({ action: "unsubscribe", id: "c1" });
    socket().emit({ type: "snapshot", id: "c1", data: {} });
    expect(snapshots).toEqual([]);
  });
});

describe("StreamClient replay", () => {
  it("starts a replay, tracks the cursor from snapshots and resumes there after a reconnect", () => {
    const { client, socket, timers, snapshots } = setup();
    client.start();
    socket().open();
    client.replay("c1", "2026-09-29", { ppr: 1, interval: 60 }, { speed: 25, autoplay: true });
    expect(socket().sent.at(-1)).toEqual({ action: "replay", id: "c1", date: "2026-09-29", ppr: 1, interval: 60,
                                           speed: 25, autoplay: true });
    socket().emit({ type: "snapshot", id: "c1", data: { ready: true, replay: { cursor_ms: 123456, playing: false } } });
    socket().emit({ type: "snapshot", id: "c1", data: { ready: true } });          // stale live snapshot: ignored
    expect(snapshots).toHaveLength(1);
    socket().close(1006);
    timers.shift()!();
    socket().open();
    expect(socket().sent.at(-1)).toEqual({ action: "replay", id: "c1", date: "2026-09-29", ppr: 1, interval: 60,
                                           speed: 25, autoplay: false, at_ms: 123456 });
  });

  it("same-day settings changes keep speed and position; controls go to the server", () => {
    const { client, socket } = setup();
    client.start();
    socket().open();
    client.replay("c1", "2026-09-29", { ppr: 1, interval: 60 }, { speed: 5 });
    socket().emit({ type: "snapshot", id: "c1", data: { ready: true, replay: { cursor_ms: 99, playing: true } } });
    client.replay("c1", "2026-09-29", { ppr: 2, interval: 300 });
    expect(socket().sent.at(-1)).toMatchObject({ action: "replay", ppr: 2, interval: 300, speed: 5, autoplay: true, at_ms: 99 });
    client.replayControl("c1", "step", { unit: "candle" });
    expect(socket().sent.at(-1)).toEqual({ action: "replay_control", id: "c1", command: "step", unit: "candle" });
    client.subscribe("c1", { ppr: 1, interval: 60 });                                  // back to live
    client.replayControl("c1", "play");                                                 // no-op when live
    expect(socket().sent.at(-1)).toMatchObject({ action: "subscribe", id: "c1" });
  });

  it("routes alert messages to onAlert without any subscription", () => {
    const alerts: unknown[] = [];
    const { client, socket } = setup({ onAlert: (e) => alerts.push(e) });
    client.start();
    socket().open();
    socket().emit({ type: "alert", data: { id: 5, message: "Breakout: price crosses above 24500" } });
    expect(alerts).toEqual([{ id: 5, message: "Breakout: price crosses above 24500" }]);
  });
});

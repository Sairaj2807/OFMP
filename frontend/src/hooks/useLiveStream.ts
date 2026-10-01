"use client";

// Owns the single /ws/v1/stream connection for the terminal: publishes
// snapshots to the snapshot bus, keeps one subscription per visible chart in
// sync with its settings (live, or a server-side replay of a stored session),
// and refreshes the session when the socket reports it expired (close 4401).
import { useEffect, useRef } from "react";
import { create } from "zustand";
import { useShallow } from "zustand/react/shallow";

import { api } from "@/lib/api";
import { snapshotBus } from "@/lib/snapshots";
import { alertActions } from "./useAlerts";
import { StreamClient, defaultStreamUrl } from "@/lib/stream";
import type { ReplayMeta } from "@/lib/types";
import { useSession } from "@/stores/session";
import { CHART_IDS, useTerminal } from "@/stores/terminal";

export interface ChartStats {
  delta: number;
  cvd: number;
  volume: number;
}

/** Per-chart figures for panels outside the chart (bottom bar). */
export const useLiveStats = create<{ stats: Record<string, ChartStats | null>; set: (id: string, s: ChartStats | null) => void }>()(
  (set) => ({
    stats: {},
    set: (id, s) => set((st) => ({ stats: { ...st.stats, [id]: s } })),
  }),
);

/** Latest replay position per chart id (drives the replay bar). */
export const useReplayMeta = create<{ meta: Record<string, ReplayMeta | undefined>; set: (id: string, m?: ReplayMeta) => void }>()(
  (set) => ({
    meta: {},
    set: (id, m) => set((st) => (st.meta[id] === m ? st : { meta: { ...st.meta, [id]: m } })),
  }),
);

/** The live connection, for components that send controls (replay bar). */
export const streamRef: { current: StreamClient | null } = { current: null };

const keyOf = (c: { id: string; ppr: number; interval: number; mode: string; replayDate: string | null }) =>
  [c.id, c.ppr, c.interval, c.mode, c.mode === "replay" ? c.replayDate : ""].join("|");

export function useLiveStream(enabled: boolean): void {
  const sent = useRef(new Map<string, string>()); // id -> settings key last sent
  const charts = useTerminal(useShallow((s) => s.charts.slice(0, s.layout).map(keyOf)));

  useEffect(() => {
    if (!enabled) return;
    const { setStream, setFeed, setIntervals } = useSession.getState();
    const c = new StreamClient({
      url: defaultStreamUrl,
      onSnapshot: (id, snap) => {
        snapshotBus.publish(id, snap);
        if (snap.replay) {
          useReplayMeta.getState().set(id, snap.replay);
        } else if (snap.status) {
          setFeed({ connected: snap.status.connected, error: snap.status.error, symbol: snap.status.symbol,
                    tickSize: snap.status.tick_size, lotSize: snap.status.lot_size });
        }
      },
      onWelcome: (data) => data.intervals && setIntervals(data.intervals),
      onStatus: (status) => setStream(status),
      onServerError: (code, message, id) => {
        if (id && code === "REPLAY_UNAVAILABLE") {
          // the chosen day cannot be replayed: fall back to live rather than show an empty chart
          useTerminal.getState().updateChart(id, { mode: "live" });
          useSession.getState().setNotice(`Replay unavailable: ${message}`);
        }
      },
      onAlert: (event) => alertActions.receive(event),
      onUnauthenticated: () => api.refresh(),
    });
    streamRef.current = c;
    sent.current.clear();
    c.start();
    return () => {
      c.stop();
      streamRef.current = null;
    };
  }, [enabled]);

  useEffect(() => {
    const c = streamRef.current;
    if (!c) return;
    const wanted = new Map(charts.map((key) => [key.split("|")[0], key]));
    const all = useTerminal.getState().charts;
    for (const [id, key] of wanted) {
      if (sent.current.get(id) === key) continue; // unchanged chart: nothing to send
      sent.current.set(id, key);
      const cfg = all.find((x) => x.id === id)!;
      if (cfg.mode === "replay" && cfg.replayDate) {
        c.replay(id, cfg.replayDate, { ppr: cfg.ppr, interval: cfg.interval });
      } else {
        useReplayMeta.getState().set(id, undefined);
        c.subscribe(id, { ppr: cfg.ppr, interval: cfg.interval });
      }
    }
    for (const id of CHART_IDS) {
      if (!wanted.has(id) && sent.current.has(id)) {
        sent.current.delete(id);
        c.unsubscribe(id);
        snapshotBus.forget(id);
      }
    }
  }, [charts, enabled]);
}

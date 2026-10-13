"use client";

// One footprint chart. Creates a FootprintController (browser-only chart
// library, loaded on demand), feeds it snapshots from the snapshot bus and
// forwards setting changes; all drawing happens inside the controller, so
// the 2 Hz stream never re-renders this component.
//
// Multi-day (live, "N days + today"): finished sessions load one day at a time
// over HTTP as the user scrolls left; today is today's bars from the server
// merged with every live snapshot window (lib/chart/footprintSessions).
import { useEffect, useRef, useState } from "react";

import { api } from "@/lib/api";
import { type BarStats, FootprintController } from "@/lib/chart/controller";
import { buildSessions, effectiveDays, FootprintHistoryLoader, mergeBars } from "@/lib/chart/footprintSessions";
import { snapshotBus } from "@/lib/snapshots";
import type { ChartSnapshot, EngineBar } from "@/lib/types";
import type { ChartConfig, Units } from "@/stores/terminal";

interface Props {
  config: ChartConfig;
  units: Units;
  lotSize: number;
  tickSize: number;
  onStats?: (stats: BarStats | null) => void;
}

const RETRY_MS = 5000;

export function FootprintChart({ config, units, lotSize, tickSize, onStats }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const tooltipRef = useRef<HTMLDivElement>(null);
  const emptyRef = useRef<HTMLDivElement>(null);
  const statusRef = useRef<HTMLDivElement>(null);
  const controllerRef = useRef<FootprintController | null>(null);
  const [ready, setReady] = useState(false);
  const onStatsRef = useRef(onStats);
  const { id, cellStyle, displayMode, showPoc, showValueArea, showImbalances, ppr, interval, mode, footprintDays,
          showTable, tableRows } = config;
  const tableKey = tableRows.join(",");
  const days = mode === "live" ? effectiveDays(footprintDays, interval) : 0;

  useEffect(() => {
    onStatsRef.current = onStats;
  }, [onStats]);

  // Create once per chart id. Initial display/unit values are read from the
  // latest render; later changes arrive through the effects below.
  const initial = useRef({ display: { cellStyle, displayMode, showPoc, showValueArea, showImbalances }, units: { units, lotSize, tickSize } });
  useEffect(() => {
    initial.current = { display: { cellStyle, displayMode, showPoc, showValueArea, showImbalances }, units: { units, lotSize, tickSize } };
  });

  useEffect(() => {
    const container = containerRef.current;
    const tooltip = tooltipRef.current;
    const empty = emptyRef.current;
    if (!container || !tooltip || !empty) return;
    let disposed = false;

    Promise.all([
      import("@/vendor/openalgo-charts/openalgo-charts.mjs"),
      import("@/vendor/openalgo-charts/openalgo-charts.profile.mjs"),
    ]).then(([chart, profile]) => {
      if (disposed) return;
      controllerRef.current = new FootprintController(container, tooltip, empty, { chart, profile },
        initial.current.display, initial.current.units, (s) => onStatsRef.current?.(s));
      setReady(true);
    });

    return () => {
      disposed = true;
      controllerRef.current?.destroy();
      controllerRef.current = null;
      setReady(false);
    };
  }, [id]);

  // Feed the controller: the live window as is, or sessions when earlier days are shown.
  useEffect(() => {
    const ctl = controllerRef.current;
    const status = statusRef.current;
    if (!ready || !ctl || !status) return;
    const showStatus = (text: string, title = "") => {
      status.textContent = text;
      status.title = title;
      status.hidden = !text;
      status.dataset.kind = "";
    };
    showStatus("");
    ctl.requestFit();
    if (!days) {
      ctl.onViewport = () => {};
      return snapshotBus.subscribe(id, (snap) => ctl.apply(snap));
    }

    const loader = new FootprintHistoryLoader({ sessions: api.footprintSessions, day: api.footprintDay }, interval, ppr, days);
    const today = new Map<number, EngineBar>();
    let rowSize = 0;
    let started = false;
    let latest: ChartSnapshot | null = null;
    let failedAt = 0;

    // a snapshot of other settings (before the stream re-subscribes) is never merged
    const fits = (snap: ChartSnapshot) => !!snap.chart && snap.mode !== "replay" && snap.chart.interval_sec === interval
      && (!rowSize || snap.chart.row_size === rowSize);
    const redraw = () => {
      if (loader.stale() || !rowSize) return;
      const sessions = buildSessions(loader.days, loader.liveDate ? { date: loader.liveDate, bars: today } : null);
      ctl.applySessions(sessions, rowSize);
    };
    const merge = (snap: ChartSnapshot) => {
      if (!snap.chart || !loader.liveDate) return;
      rowSize = snap.chart.row_size;
      mergeBars(today, snap.chart.bars, loader.liveDate);
    };
    const loadOlder = () => {
      if (loader.loading || loader.exhausted || Date.now() - failedAt < RETRY_MS) return;
      showStatus("Loading earlier session…");
      loader.loadOlder().then((added) => {
        if (loader.stale()) return;
        showStatus("");
        if (added) redraw();
      }, () => {
        if (loader.stale()) return;
        failedAt = Date.now();
        showStatus("Could not load the earlier session; retrying when you scroll");
      });
    };
    ctl.onViewport = (near) => {
      if (!started || !near) {
        if (started && status.dataset.kind === "start") showStatus("");
        return;
      }
      if (!loader.exhausted) return loadOlder();
      const excluded = loader.excluded.map((e) => `${e.date}: ${e.reason}`).join("\n");
      showStatus(loader.available.length >= loader.cap ? `Showing ${loader.cap} earlier sessions (the most for this interval)`
        : "Start of recorded history", excluded ? `Not shown:\n${excluded}` : "");
      status.dataset.kind = "start";
    };

    const unsubscribe = snapshotBus.subscribe(id, (snap) => {
      if (!fits(snap)) return;
      if (!started) {
        latest = snap;
        ctl.apply(snap);              // the live window until the sessions arrive
        return;
      }
      merge(snap);
      redraw();
    });

    showStatus("Loading sessions…");
    loader.start().then(() => {
      if (loader.stale()) return;
      started = true;
      showStatus("");
      if (loader.today && loader.liveDate) {
        rowSize = loader.today.row_size;
        mergeBars(today, loader.today.bars, loader.liveDate);
      }
      if (latest && fits(latest)) merge(latest);
      if (!rowSize && loader.days.size) rowSize = [...loader.days.values()][0].row_size;
      redraw();
    }, () => {
      if (loader.stale()) return;
      showStatus("Earlier sessions are unavailable; showing today");
      started = false;
    });

    return () => {
      loader.dispose();
      unsubscribe();
      ctl.onViewport = () => {};
    };
  }, [ready, id, days, interval, ppr]);

  useEffect(() => {
    controllerRef.current?.setDisplay({ cellStyle, displayMode, showPoc, showValueArea, showImbalances });
  }, [cellStyle, displayMode, showPoc, showValueArea, showImbalances]);

  useEffect(() => {
    controllerRef.current?.setUnits({ units, lotSize, tickSize });
  }, [units, lotSize, tickSize]);

  useEffect(() => {
    if (ready) controllerRef.current?.setTable(showTable, tableKey ? (tableKey.split(",") as typeof tableRows) : []);
  }, [ready, showTable, tableKey]);

  return (
    <div className="relative h-full w-full" data-testid={`chart-${id}`}>
      {/* the chart library sets its own role/label/tabindex on this container */}
      <div ref={containerRef} className="absolute inset-0" />
      <div ref={emptyRef} className="pointer-events-none absolute inset-0 grid place-items-center text-muted">
        Waiting for the first trade…
      </div>
      <div ref={statusRef} hidden data-testid={`footprint-status-${id}`} role="status"
           className="absolute left-2 top-7 z-10 rounded border border-line bg-raised/90 px-2 py-0.5 text-[11px] text-muted" />
      {/* top-left, under the session date label: the bottom holds the footprint table */}
      <div
        ref={tooltipRef}
        hidden
        className="pointer-events-none absolute z-10 min-w-48 rounded border border-line-strong bg-raised/95 px-2.5 py-2 text-[12px] shadow-lg"
      />
    </div>
  );
}

"use client";

// One footprint chart. Creates a FootprintController (browser-only chart
// library, loaded on demand), feeds it snapshots from the snapshot bus and
// forwards setting changes; all drawing happens inside the controller, so
// the 2 Hz stream never re-renders this component.
import { useEffect, useRef } from "react";

import { type BarStats, FootprintController } from "@/lib/chart/controller";
import { snapshotBus } from "@/lib/snapshots";
import type { ChartConfig, Units } from "@/stores/terminal";

interface Props {
  config: ChartConfig;
  units: Units;
  lotSize: number;
  tickSize: number;
  onStats?: (stats: BarStats | null) => void;
}

export function FootprintChart({ config, units, lotSize, tickSize, onStats }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const tooltipRef = useRef<HTMLDivElement>(null);
  const emptyRef = useRef<HTMLDivElement>(null);
  const controllerRef = useRef<FootprintController | null>(null);
  const onStatsRef = useRef(onStats);
  const { id, cellStyle, displayMode, showPoc, showValueArea, showImbalances, ppr, interval } = config;

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
    let unsubscribe = () => {};

    Promise.all([
      import("@/vendor/openalgo-charts/openalgo-charts.mjs"),
      import("@/vendor/openalgo-charts/openalgo-charts.profile.mjs"),
    ]).then(([chart, profile]) => {
      if (disposed) return;
      const controller = new FootprintController(container, tooltip, empty, { chart, profile },
        initial.current.display, initial.current.units, (s) => onStatsRef.current?.(s));
      controllerRef.current = controller;
      unsubscribe = snapshotBus.subscribe(id, (snap) => controller.apply(snap));
    });

    return () => {
      disposed = true;
      unsubscribe();
      controllerRef.current?.destroy();
      controllerRef.current = null;
    };
  }, [id]);

  useEffect(() => {
    controllerRef.current?.setDisplay({ cellStyle, displayMode, showPoc, showValueArea, showImbalances });
  }, [cellStyle, displayMode, showPoc, showValueArea, showImbalances]);

  useEffect(() => {
    controllerRef.current?.setUnits({ units, lotSize, tickSize });
  }, [units, lotSize, tickSize]);

  useEffect(() => {
    controllerRef.current?.requestFit();
  }, [ppr, interval]);

  return (
    <div className="relative h-full w-full" data-testid={`chart-${id}`}>
      {/* the chart library sets its own role/label/tabindex on this container */}
      <div ref={containerRef} className="absolute inset-0" />
      <div ref={emptyRef} className="pointer-events-none absolute inset-0 grid place-items-center text-muted">
        Waiting for the first trade…
      </div>
      <div
        ref={tooltipRef}
        hidden
        className="pointer-events-none absolute z-10 min-w-48 rounded border border-line-strong bg-raised/95 px-2.5 py-2 text-[12px] shadow-lg"
      />
    </div>
  );
}

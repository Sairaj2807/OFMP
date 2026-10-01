"use client";

import { type ChartView, useTerminal } from "@/stores/terminal";

const VIEWS: { view: ChartView; label: string; icon: React.ReactNode }[] = [
  {
    view: "footprint", label: "Order-flow footprint",
    icon: (
      <svg viewBox="0 0 20 20" className="h-5 w-5" aria-hidden fill="none" stroke="currentColor" strokeWidth="1.4">
        <rect x="3" y="4" width="5" height="12" rx="0.5" /><rect x="12" y="2" width="5" height="14" rx="0.5" />
        <path d="M5.5 2v2M5.5 16v2M14.5 1v1M14.5 16v3" />
      </svg>
    ),
  },
  {
    view: "profile", label: "Market profile",
    icon: (
      <svg viewBox="0 0 20 20" className="h-5 w-5" aria-hidden fill="none" stroke="currentColor" strokeWidth="1.4">
        <path d="M3 3h3M3 6h7M3 9h11M3 12h9M3 15h5M3 18h2" strokeLinecap="round" />
      </svg>
    ),
  },
];

/** Left sidebar: switches the focused chart between footprint and market profile (each chart keeps its own). */
export function ViewRail() {
  const activeId = useTerminal((s) => s.activeChartId);
  const view = useTerminal((s) => s.charts.find((c) => c.id === s.activeChartId)?.view ?? "footprint");
  const update = useTerminal((s) => s.updateChart);

  return (
    <nav aria-label="Chart type" className="flex w-11 shrink-0 flex-col items-center gap-1 border-r border-line bg-panel py-2">
      {VIEWS.map((v) => (
        <button key={v.view} type="button" aria-pressed={view === v.view} title={`${v.label} (M toggles)`}
                aria-label={`${v.label} on chart ${activeId}`}
                onClick={() => update(activeId, { view: v.view })}
                className={`grid h-9 w-9 place-items-center rounded ${
                  view === v.view ? "bg-accent/20 text-fg ring-1 ring-accent/60" : "text-muted hover:bg-raised hover:text-fg"}`}>
          {v.icon}
        </button>
      ))}
      <span className="mt-1 text-[10px] uppercase tracking-wide text-muted" aria-hidden>{activeId}</span>
    </nav>
  );
}

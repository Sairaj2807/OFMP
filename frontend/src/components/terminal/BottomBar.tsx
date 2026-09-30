"use client";

import { useEffect, useState } from "react";

import { useLiveStats } from "@/hooks/useLiveStream";
import { intervalLabel, price, signed } from "@/lib/format";
import { snapshotBus } from "@/lib/snapshots";
import { useSession } from "@/stores/session";
import { useTerminal } from "@/stores/terminal";

function Stat({ label, value, tone }: { label: string; value: string; tone?: "buy" | "sell" }) {
  return (
    <div className="flex flex-col px-3">
      <span className="text-[10px] uppercase tracking-wider text-muted">{label}</span>
      <span className={`num text-[13px] ${tone === "buy" ? "text-buy" : tone === "sell" ? "text-sell" : "text-fg"}`}>{value}</span>
    </div>
  );
}

/** Figures for the focused chart: last price, bar volume/delta, CVD, and how BUY/SELL are decided. */
export function BottomBar() {
  const active = useTerminal((s) => s.charts.find((c) => c.id === s.activeChartId));
  const units = useTerminal((s) => s.units);
  const lotSize = useSession((s) => s.feed.lotSize);
  const stats = useLiveStats((s) => (active ? s.stats[active.id] : null));
  const [ltp, setLtp] = useState<number | null>(null);
  const [sideBasis, setSideBasis] = useState<string | null>(null);

  useEffect(() => {
    if (!active) return;
    return snapshotBus.subscribe(active.id, (snap) => {
      setLtp(snap.quote?.ltp ?? null);
      setSideBasis(snap.source?.side_basis ?? null);
    });
  }, [active]);

  const d = units === "lots" ? lotSize : 1;
  const tone = (v: number | undefined) => (v === undefined ? undefined : v >= 0 ? "buy" : "sell");
  return (
    <footer className="flex h-12 shrink-0 items-center divide-x divide-line border-t border-line bg-panel" aria-label="Session figures">
      <Stat label={`Chart ${active?.id ?? ""} · ${active ? intervalLabel(active.interval) : ""}`} value={price(ltp)} />
      <Stat label={`Bar volume · ${units}`} value={stats ? signed(stats.volume / d).replace("+", "") : "—"} />
      <Stat label={`Bar delta · ${units}`} value={stats ? signed(stats.delta / d) : "—"} tone={tone(stats?.delta)} />
      <Stat label={`CVD since start · ${units}`} value={stats ? signed(stats.cvd / d) : "—"} tone={tone(stats?.cvd)} />
      <div className="ml-auto px-3 text-[11px] text-muted" title="BUY/SELL are inferred per trade by the validated midpoint rule (vtrender_reconstruction v1).">
        Sides: {sideBasis ?? "—"}
      </div>
    </footer>
  );
}

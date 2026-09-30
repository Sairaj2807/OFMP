"use client";

import { useCallback } from "react";

import { FootprintChart } from "@/components/chart/FootprintChart";
import { Button, Select } from "@/components/ui/controls";
import { type ChartStats, useLiveStats } from "@/hooks/useLiveStream";
import { intervalLabel } from "@/lib/format";
import { useSession } from "@/stores/session";
import { type CellStyle, type ChartConfig, type DisplayMode, useTerminal } from "@/stores/terminal";
import { ReplayBar, loadReplaySessions } from "./ReplayBar";

export function ChartPanel({ config }: { config: ChartConfig }) {
  const intervals = useSession((s) => s.intervals);
  const lotSize = useSession((s) => s.feed.lotSize);
  const tickSize = useSession((s) => s.feed.tickSize);
  const units = useTerminal((s) => s.units);
  const active = useTerminal((s) => s.activeChartId === config.id);
  const setActive = useTerminal((s) => s.setActiveChart);
  const update = useTerminal((s) => s.updateChart);
  const setStats = useLiveStats((s) => s.set);
  const onStats = useCallback((st: ChartStats | null) => setStats(config.id, st), [config.id, setStats]);
  const setNotice = useSession((s) => s.setNotice);

  const toReplay = async () => {
    const date = config.replayDate ?? (await loadReplaySessions())[0]?.date;
    if (!date) {
      setNotice("No recorded sessions to replay yet.");
      return;
    }
    update(config.id, { mode: "replay", replayDate: date });
  };

  return (
    <section
      className={`flex h-full flex-col bg-panel ${active ? "ring-1 ring-inset ring-accent/40" : ""}`}
      onPointerDown={() => !active && setActive(config.id)}
      aria-label={`Chart ${config.id}`}
    >
      <div className="flex h-8 shrink-0 items-center gap-1 overflow-x-auto border-b border-line px-2">
        <div className="flex items-center gap-0.5" role="group" aria-label="Data source">
          <Button active={config.mode === "live"} onClick={() => update(config.id, { mode: "live" })}>Live</Button>
          <Button active={config.mode === "replay"} onClick={toReplay}>Replay</Button>
        </div>
        <span className="mx-1 h-4 w-px bg-line" aria-hidden />
        <div className="flex items-center gap-0.5" role="group" aria-label="Interval">
          {intervals.map((sec) => (
            <Button key={sec} active={config.interval === sec} onClick={() => update(config.id, { interval: sec })}>
              {intervalLabel(sec)}
            </Button>
          ))}
        </div>
        <span className="mx-1 h-4 w-px bg-line" aria-hidden />
        <Select label="Price per row" value={config.ppr} onChange={(e) => update(config.id, { ppr: Number(e.target.value) })}>
          {[1, 2, 3, 4, 5].map((n) => <option key={n} value={n}>{n} ₹/row</option>)}
        </Select>
        <Select label="Cell style" value={config.cellStyle}
                onChange={(e) => update(config.id, { cellStyle: e.target.value as CellStyle })}>
          <option value="profile">Profile</option>
          <option value="ladder">Ladder</option>
          <option value="heatmap">Heatmap</option>
        </Select>
        <Select label="Values" value={config.displayMode}
                onChange={(e) => update(config.id, { displayMode: e.target.value as DisplayMode })}>
          <option value="bidask">Sell × Buy</option>
          <option value="delta">Delta</option>
          <option value="volume">Volume</option>
        </Select>
        <span className="mx-1 h-4 w-px bg-line" aria-hidden />
        <Button active={config.showPoc} onClick={() => update(config.id, { showPoc: !config.showPoc })}>POC</Button>
        <Button active={config.showValueArea} onClick={() => update(config.id, { showValueArea: !config.showValueArea })}>VA</Button>
        <Button active={config.showImbalances} onClick={() => update(config.id, { showImbalances: !config.showImbalances })}
                title="Stacked imbalances (engine rule v1, ratio 3)">Imb</Button>
      </div>
      {config.mode === "replay" && <ReplayBar config={config} />}
      <div className="min-h-0 flex-1">
        <FootprintChart config={config} units={units} lotSize={lotSize} tickSize={tickSize} onStats={onStats} />
      </div>
    </section>
  );
}

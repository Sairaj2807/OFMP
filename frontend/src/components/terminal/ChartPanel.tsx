"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { FootprintChart } from "@/components/chart/FootprintChart";
import { MultiDayProfileChart } from "@/components/chart/MultiDayProfileChart";
import { Button, Select } from "@/components/ui/controls";
import { type ChartStats, useLiveStats } from "@/hooks/useLiveStream";
import { dayCap } from "@/lib/chart/footprintSessions";
import { TABLE_ROW_LABELS } from "@/lib/chart/footprintTable";
import { intervalLabel } from "@/lib/format";
import { useSession } from "@/stores/session";
import {
  type CellStyle, type ChartConfig, type DisplayMode, FOOTPRINT_DAYS, PROFILE_DAYS, PROFILE_ROWS, TABLE_ROWS, useTerminal,
} from "@/stores/terminal";
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
        <span className="px-1 text-[11px] uppercase tracking-wide text-muted" aria-label="Chart type">
          {config.view === "profile" ? "Profile" : "Footprint"}
        </span>
        {config.view === "profile" ? (
          <>
            <Select label="Profile row size" value={config.profileRow}
                    onChange={(e) => update(config.id, { profileRow: Number(e.target.value) })}>
              {PROFILE_ROWS.map((n) => <option key={n} value={n}>{n} pt/row</option>)}
            </Select>
            <Select label="Profile sessions" value={config.profileDays} disabled={config.mode === "replay"}
                    title={config.mode === "replay" ? "Replay shows one session" : "Finished sessions shown before today"}
                    onChange={(e) => update(config.id, { profileDays: Number(e.target.value) })}>
              {PROFILE_DAYS.map((n) => <option key={n} value={n}>{n === 0 ? "Today" : `${n} days + today`}</option>)}
            </Select>
            <div className="flex items-center gap-0.5" role="group" aria-label="Profile layout">
              <Button active={config.profileMode === "stacked"} onClick={() => update(config.id, { profileMode: "stacked" })}
                      title="Stacked: each row's letters packed left (the profile shape)">Stacked</Button>
              <Button active={config.profileMode === "split"} onClick={() => update(config.id, { profileMode: "split" })}
                      title="Split: one column per period, a time trace with each period's open and close">Split</Button>
            </div>
            <Button active={config.showPoc} onClick={() => update(config.id, { showPoc: !config.showPoc })}
                    title="TPO and volume points of control">POC</Button>
            <Button active={config.showValueArea} onClick={() => update(config.id, { showValueArea: !config.showValueArea })}
                    title="Value area (70% of TPOs)">VA</Button>
          </>
        ) : (
          <>
            <div className="flex items-center gap-0.5" role="group" aria-label="Interval">
              {intervals.map((sec) => (
                <Button key={sec} active={config.interval === sec} onClick={() => update(config.id, { interval: sec })}>
                  {intervalLabel(sec)}
                </Button>
              ))}
            </div>
            <span className="mx-1 h-4 w-px bg-line" aria-hidden />
            <Select label="Footprint sessions" value={Math.min(config.footprintDays, dayCap(config.interval))}
                    disabled={config.mode === "replay"}
                    title={config.mode === "replay" ? "Replay shows one session"
                      : `Finished sessions to the left of today (at most ${dayCap(config.interval)} at ${intervalLabel(config.interval)}); CVD restarts each session`}
                    onChange={(e) => update(config.id, { footprintDays: Number(e.target.value) })}>
              {FOOTPRINT_DAYS.filter((n) => n <= dayCap(config.interval)).map((n) =>
                <option key={n} value={n}>{n === 0 ? "Today" : `${n} days + today`}</option>)}
            </Select>
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
            <span className="mx-1 h-4 w-px bg-line" aria-hidden />
            <Button active={config.showTable} onClick={() => update(config.id, { showTable: !config.showTable })}
                    title="Table under the candles: Volume, VPOC, VPOC Vol, COT (buy − sell), OI, dOI">Table</Button>
            <TableRowsMenu config={config} onChange={(tableRows) => update(config.id, { tableRows, showTable: true })} />
          </>
        )}
      </div>
      {config.mode === "replay" && <ReplayBar config={config} />}
      <div className="min-h-0 flex-1">
        {config.view === "profile"
          ? <MultiDayProfileChart key="profile" config={config} units={units} lotSize={lotSize} />
          : <FootprintChart key="footprint" config={config} units={units} lotSize={lotSize} tickSize={tickSize}
                            onStats={onStats} />}
      </div>
    </section>
  );
}

/** Which footprint-table rows to show (saved per chart). A fixed-position popup: the toolbar scrolls
 *  horizontally, which would clip an absolutely positioned one. */
function TableRowsMenu({ config, onChange }: { config: ChartConfig; onChange: (rows: ChartConfig["tableRows"]) => void }) {
  const [at, setAt] = useState<{ top: number; right: number } | null>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const popupRef = useRef<HTMLFieldSetElement>(null);

  useEffect(() => {
    if (!at) return;
    const close = (e: Event) => {
      const t = e.target as Node;
      if (!popupRef.current?.contains(t) && !buttonRef.current?.contains(t)) setAt(null);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setAt(null);
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", close);
      document.removeEventListener("keydown", onKey);
    };
  }, [at]);

  const toggle = () => {
    const box = buttonRef.current?.getBoundingClientRect();
    setAt(at || !box ? null : { top: box.bottom + 4, right: window.innerWidth - box.right });
  };
  return (
    <>
      <button ref={buttonRef} type="button" aria-label="Table rows" aria-expanded={!!at} aria-haspopup="true"
              onClick={toggle} title="Choose the table's rows"
              className="h-6 rounded px-1.5 text-[12px] text-fg-2 transition-colors hover:bg-raised hover:text-fg">▾</button>
      {at && (
        <fieldset ref={popupRef} style={{ top: at.top, right: at.right }}
                  className="fixed z-50 w-36 rounded border border-line-strong bg-raised p-2 text-[12px] shadow-lg">
          <legend className="sr-only">Table rows</legend>
          {TABLE_ROWS.map((row) => (
            <label key={row} className="flex cursor-pointer items-center gap-2 py-0.5">
              <input type="checkbox" checked={config.tableRows.includes(row)}
                     onChange={(e) => onChange(TABLE_ROWS.filter((r) => (r === row ? e.target.checked : config.tableRows.includes(r))))} />
              {TABLE_ROW_LABELS[row]}
            </label>
          ))}
        </fieldset>
      )}
    </>
  );
}

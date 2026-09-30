"use client";

import { useEffect, useRef, useState } from "react";

import { Button, Select } from "@/components/ui/controls";
import { streamRef, useReplayMeta } from "@/hooks/useLiveStream";
import { api } from "@/lib/api";
import type { ReplaySessionInfo } from "@/lib/types";
import { type ChartConfig, useTerminal } from "@/stores/terminal";

let sessionsCache: Promise<ReplaySessionInfo[]> | null = null;
/** Stored sessions, newest first (fetched once per page load). */
export function loadReplaySessions(): Promise<ReplaySessionInfo[]> {
  sessionsCache ??= api.replaySessions()
    .then((r) => [...r.data].sort((a, b) => b.date.localeCompare(a.date)))
    .catch(() => {
      sessionsCache = null;
      return [];
    });
  return sessionsCache;
}

const IST = "Asia/Kolkata";
const clock = (ms: number) =>
  new Date(ms).toLocaleTimeString("en-GB", { timeZone: IST, hour12: false });

/** Server-side replay controls for one chart. */
export function ReplayBar({ config }: { config: ChartConfig }) {
  const meta = useReplayMeta((s) => s.meta[config.id]);
  const update = useTerminal((s) => s.updateChart);
  const [sessions, setSessions] = useState<ReplaySessionInfo[]>([]);
  const [scrub, setScrub] = useState<number | null>(null); // slider position while dragging
  const seekTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    let alive = true;
    loadReplaySessions().then((s) => alive && setSessions(s));
    return () => {
      alive = false;
    };
  }, []);

  const control = (command: "play" | "pause" | "speed" | "seek" | "step", arg?: { value?: number; unit?: "trade" | "candle" }) =>
    streamRef.current?.replayControl(config.id, command, arg);

  const onScrub = (value: number) => {
    setScrub(value);
    if (seekTimer.current) clearTimeout(seekTimer.current);
    seekTimer.current = setTimeout(() => {
      control("seek", { value });
      setScrub(null);
    }, 120);
  };

  const position = scrub ?? meta?.cursor_ms ?? 0;
  return (
    <div className="flex h-8 shrink-0 items-center gap-2 border-b border-line bg-panel-2 px-2 text-[12px]" role="group"
         aria-label={`Replay controls ${config.id}`}>
      <span className="rounded bg-warn/15 px-1.5 text-[10px] font-semibold uppercase tracking-wider text-warn">Replay</span>
      <Select label="Session" value={config.replayDate ?? ""}
              onChange={(e) => update(config.id, { replayDate: e.target.value })}>
        {sessions.length === 0 && <option value={config.replayDate ?? ""}>{config.replayDate ?? "No sessions"}</option>}
        {sessions.map((s) => (
          <option key={s.date} value={s.date}>{s.date}{s.trades != null ? ` · ${s.trades.toLocaleString()} trades` : ""}</option>
        ))}
      </Select>
      <Button onClick={() => control("step", { unit: "trade" })} disabled={!meta} title="Step one trade"
              aria-label="Step one trade">⇥ trade</Button>
      <Button onClick={() => control("step", { unit: "candle" })} disabled={!meta} title="Step to the end of the next candle"
              aria-label="Step one candle">⇥ candle</Button>
      <Button active={meta?.playing} disabled={!meta} onClick={() => control(meta?.playing ? "pause" : "play")}
              aria-label={meta?.playing ? "Pause replay" : "Play replay"}>
        {meta?.playing ? "❚❚ Pause" : "▶ Play"}
      </Button>
      <Select label="Replay speed" value={meta?.speed ?? 10} disabled={!meta}
              onChange={(e) => control("speed", { value: Number(e.target.value) })}>
        {(meta?.speeds ?? [1, 2, 5, 10, 25, 50]).map((s) => <option key={s} value={s}>{s}×</option>)}
      </Select>
      <input
        type="range"
        aria-label="Replay position"
        className="min-w-24 flex-1 accent-[var(--warn)]"
        min={meta?.start_ms ?? 0}
        max={meta?.end_ms ?? 1}
        step={1000}
        value={position}
        disabled={!meta}
        onChange={(e) => onScrub(Number(e.target.value))}
      />
      <span className="num w-20 text-right text-fg" aria-label="Replay clock">{meta ? clock(position) : "--:--:--"}</span>
      <span className="num w-28 text-right text-muted" aria-label="Replay progress">
        {meta ? `${meta.index.toLocaleString()} / ${meta.total.toLocaleString()}` : ""}
      </span>
    </div>
  );
}

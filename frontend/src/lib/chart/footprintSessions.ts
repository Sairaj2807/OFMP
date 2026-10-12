// Multi-day footprint: earlier sessions continue to the left of today. Pure
// functions and a small loader (no DOM), unit tested.
//
// - Finished sessions come one day at a time from GET /api/v1/market/footprint/history
//   (immutable, browser-cached); older days load as the user scrolls back.
// - Today = today's bars from the server ("today so far") merged with every live
//   snapshot window seen since (the live window wins for the same bar), so bars that
//   scroll out of the 60-bar live window are never lost.
// - Each session keeps its own bars: the chart gives each its own footprint layer, so
//   CVD restarts at 0 at every session start (session CVD).
import type { EngineBar, FootprintDay, FootprintSessions } from "../types";

/** Days that may be shown before today, by display interval (owner decision 2026-10-12). */
export const FOOTPRINT_DAY_CAPS: Record<number, number> = { 60: 5, 180: 10 };
export const DEFAULT_DAY_CAP = 20;
export const dayCap = (intervalSec: number) => FOOTPRINT_DAY_CAPS[intervalSec] ?? DEFAULT_DAY_CAP;
export const effectiveDays = (wanted: number, intervalSec: number) => Math.max(0, Math.min(wanted, dayCap(intervalSec)));

const IST_OFFSET_SEC = 5.5 * 3600;
/** The IST calendar date (YYYY-MM-DD) of a bar time (epoch seconds). */
export function istDate(timeSec: number): string {
  return new Date((timeSec + IST_OFFSET_SEC) * 1000).toISOString().slice(0, 10);
}

export interface FootprintSession {
  date: string;
  live: boolean;
  bars: EngineBar[];          // oldest first
  cvdStart: number;           // CVD before the session's first bar (0: the session start)
}

/** Merge bars by time into `into` (later wins), keeping only bars of `date`. */
export function mergeBars(into: Map<number, EngineBar>, bars: EngineBar[], date: string): void {
  for (const b of bars) if (istDate(b.time) === date) into.set(b.time, b);
}

/** Sessions oldest first: loaded finished days (before today), then today. */
export function buildSessions(days: Map<string, FootprintDay>, today: { date: string; bars: Map<number, EngineBar> } | null):
  FootprintSession[] {
  const out: FootprintSession[] = [...days.values()]
    .filter((d) => !today || d.date < today.date)
    .sort((a, b) => a.date.localeCompare(b.date))
    .map((d) => ({ date: d.date, live: false, bars: d.bars, cvdStart: d.cvd_start }));
  if (today) {
    const bars = [...today.bars.values()].sort((a, b) => a.time - b.time);
    out.push({ date: today.date, live: true, bars, cvdStart: 0 });
  }
  return out;
}

/** The bar times where each session starts (for separators and labels). */
export const sessionStarts = (sessions: FootprintSession[]) =>
  sessions.filter((s) => s.bars.length).map((s) => ({ date: s.date, live: s.live, time: s.bars[0].time }));

export interface LoaderApi {
  sessions: (intervalSec: number) => Promise<FootprintSessions>;
  day: (date: string, intervalSec: number, ppr: number) => Promise<FootprintDay>;
}

/** Loads finished days newest first, one at a time, up to the cap. A settings change makes a
 *  new loader; `stale()` lets late responses from an old one be ignored. */
export class FootprintHistoryLoader {
  readonly days = new Map<string, FootprintDay>();
  available: string[] = [];             // finished sessions, oldest first
  excluded: FootprintSessions["excluded"] = [];
  liveDate: string | null = null;
  today: FootprintDay | null = null;
  loading = false;
  private disposed = false;

  constructor(private readonly api: LoaderApi, readonly intervalSec: number, readonly ppr: number,
              readonly wantedDays: number) {}

  dispose(): void {
    this.disposed = true;
  }

  stale(): boolean {
    return this.disposed;
  }

  get cap(): number {
    return effectiveDays(this.wantedDays, this.intervalSec);
  }

  /** The list of sessions, today so far, and the newest finished day. */
  async start(): Promise<void> {
    const list = await this.api.sessions(this.intervalSec);
    if (this.disposed) return;
    this.liveDate = list.live_date ?? null;
    this.available = list.sessions.slice(-this.cap);
    this.excluded = list.excluded ?? [];
    if (this.liveDate) {
      try {
        const today = await this.api.day(this.liveDate, this.intervalSec, this.ppr);
        if (!this.disposed) this.today = today;
      } catch {
        /* no bars today yet: the live snapshots fill today */
      }
    }
    await this.loadOlder();
  }

  /** The next older finished day, if any is left within the cap. Returns whether one was added. */
  async loadOlder(): Promise<boolean> {
    if (this.loading || this.disposed) return false;
    const next = [...this.available].reverse().find((d) => !this.days.has(d));
    if (!next) return false;
    this.loading = true;
    try {
      const day = await this.api.day(next, this.intervalSec, this.ppr);
      if (this.disposed) return false;
      this.days.set(next, day);
      return true;
    } finally {
      this.loading = false;
    }
  }

  get exhausted(): boolean {
    return this.available.every((d) => this.days.has(d));
  }
}

// Terminal layout and per-chart settings. Persisted per browser as versioned
// JSON, and synced to the signed-in user's server-side workspace (see
// hooks/useWorkspaces.ts) — the same versioned shape in both places, so an
// older saved layout is migrated, never discarded.
import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

export type CellStyle = "profile" | "ladder" | "heatmap";
export type DisplayMode = "bidask" | "delta" | "volume";
export type Layout = 1 | 2 | 4;
export type Units = "qty" | "lots";
export type ChartMode = "live" | "replay";

export interface ChartConfig {
  id: string;
  interval: number;
  ppr: number;
  cellStyle: CellStyle;
  displayMode: DisplayMode;
  showPoc: boolean;
  showValueArea: boolean;
  showImbalances: boolean;
  mode: ChartMode;
  replayDate: string | null; // session replayed when mode === "replay"
}

export const CHART_IDS = ["c1", "c2", "c3", "c4"] as const;
export const DEFAULT_INTERVALS = [60, 180, 300, 900, 1800];

const defaultChart = (id: string, interval: number): ChartConfig => ({
  id, interval, ppr: 1, cellStyle: "profile", displayMode: "bidask",
  showPoc: true, showValueArea: true, showImbalances: true, mode: "live", replayDate: null,
});

/** The persisted/synced part of the terminal state. */
export interface TerminalConfig {
  layout: Layout;
  charts: ChartConfig[];
  activeChartId: string;
  units: Units;
}

export interface TerminalState extends TerminalConfig {
  setLayout: (layout: Layout) => void;
  setActiveChart: (id: string) => void;
  updateChart: (id: string, patch: Partial<Omit<ChartConfig, "id">>) => void;
  setUnits: (units: Units) => void;
  applyConfig: (config: TerminalConfig) => void;
}

export const initialTerminal: TerminalConfig = {
  layout: 1,
  charts: [defaultChart("c1", 60), defaultChart("c2", 300), defaultChart("c3", 900), defaultChart("c4", 1800)],
  activeChartId: "c1",
  units: "qty",
};

export const STORAGE_KEY = "ofmp.terminal";
export const STORAGE_VERSION = 2;

/**
 * Brings any saved config (localStorage or a server workspace, any version)
 * to the current shape. Unknown or invalid fields fall back to defaults
 * field by field, so one bad value never discards a whole layout.
 *   v1 -> v2: charts gained mode/replayDate (existing charts become live).
 */
export function migrateConfig(raw: unknown): TerminalConfig {
  const src = (raw && typeof raw === "object" ? raw : {}) as Partial<TerminalConfig>;
  const layout: Layout = src.layout === 1 || src.layout === 2 || src.layout === 4 ? src.layout : initialTerminal.layout;
  const charts = initialTerminal.charts.map((def) => {
    const saved = Array.isArray(src.charts) ? src.charts.find((c) => c && c.id === def.id) : undefined;
    const c = { ...def, ...(saved ?? {}) } as ChartConfig;
    if (!Number.isInteger(c.ppr) || c.ppr < 1 || c.ppr > 5) c.ppr = def.ppr;
    if (!Number.isInteger(c.interval) || c.interval <= 0) c.interval = def.interval;
    if (!["profile", "ladder", "heatmap"].includes(c.cellStyle)) c.cellStyle = def.cellStyle;
    if (!["bidask", "delta", "volume"].includes(c.displayMode)) c.displayMode = def.displayMode;
    if (c.mode !== "live" && c.mode !== "replay") c.mode = "live";
    if (typeof c.replayDate !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(c.replayDate)) c.replayDate = null;
    if (c.mode === "replay" && !c.replayDate) c.mode = "live";
    for (const k of ["showPoc", "showValueArea", "showImbalances"] as const) if (typeof c[k] !== "boolean") c[k] = def[k];
    return c;
  });
  const activeChartId = CHART_IDS.includes(src.activeChartId as (typeof CHART_IDS)[number]) ? src.activeChartId! : "c1";
  const units: Units = src.units === "lots" ? "lots" : "qty";
  return { layout, charts, activeChartId, units };
}

export const toConfig = ({ layout, charts, activeChartId, units }: TerminalConfig): TerminalConfig =>
  ({ layout, charts, activeChartId, units });

export const useTerminal = create<TerminalState>()(
  persist<TerminalState, [], [], TerminalConfig>(
    (set) => ({
      ...initialTerminal,
      setLayout: (layout) =>
        set((s) => ({
          layout,
          // keep focus on a chart that is still visible
          activeChartId: CHART_IDS.indexOf(s.activeChartId as (typeof CHART_IDS)[number]) < layout ? s.activeChartId : "c1",
        })),
      setActiveChart: (activeChartId) => set({ activeChartId }),
      updateChart: (id, patch) => set((s) => ({ charts: s.charts.map((c) => (c.id === id ? { ...c, ...patch } : c)) })),
      setUnits: (units) => set({ units }),
      applyConfig: (config) => set(migrateConfig(config)),
    }),
    {
      name: STORAGE_KEY,
      version: STORAGE_VERSION,
      storage: createJSONStorage(() => localStorage),
      partialize: toConfig,
      migrate: (persisted) => migrateConfig(persisted),
    },
  ),
);

export const visibleCharts = (s: Pick<TerminalState, "layout" | "charts">) => s.charts.slice(0, s.layout);

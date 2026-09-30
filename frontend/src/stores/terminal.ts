// Terminal layout and per-chart settings, persisted per browser as versioned
// JSON so a future release can migrate old saved layouts (server-side
// workspaces replace this storage in a later phase; the shape stays).
import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

export type CellStyle = "profile" | "ladder" | "heatmap";
export type DisplayMode = "bidask" | "delta" | "volume";
export type Layout = 1 | 2 | 4;
export type Units = "qty" | "lots";

export interface ChartConfig {
  id: string;
  interval: number;
  ppr: number;
  cellStyle: CellStyle;
  displayMode: DisplayMode;
  showPoc: boolean;
  showValueArea: boolean;
  showImbalances: boolean;
}

export const CHART_IDS = ["c1", "c2", "c3", "c4"] as const;
export const DEFAULT_INTERVALS = [60, 180, 300, 900, 1800];

const defaultChart = (id: string, interval: number): ChartConfig => ({
  id, interval, ppr: 1, cellStyle: "profile", displayMode: "bidask",
  showPoc: true, showValueArea: true, showImbalances: true,
});

export interface TerminalState {
  layout: Layout;
  charts: ChartConfig[];
  activeChartId: string;
  units: Units;
  setLayout: (layout: Layout) => void;
  setActiveChart: (id: string) => void;
  updateChart: (id: string, patch: Partial<Omit<ChartConfig, "id">>) => void;
  setUnits: (units: Units) => void;
}

export const initialTerminal = {
  layout: 1 as Layout,
  charts: [defaultChart("c1", 60), defaultChart("c2", 300), defaultChart("c3", 900), defaultChart("c4", 1800)],
  activeChartId: "c1",
  units: "qty" as Units,
};

export const STORAGE_KEY = "ofmp.terminal";
export const STORAGE_VERSION = 1;

export const useTerminal = create<TerminalState>()(
  persist(
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
    }),
    {
      name: STORAGE_KEY,
      version: STORAGE_VERSION,
      storage: createJSONStorage(() => localStorage),
      partialize: ({ layout, charts, activeChartId, units }) => ({ layout, charts, activeChartId, units }),
      // v0 -> v1 and any unreadable state: fall back to defaults rather than crash.
      migrate: (persisted, version) => (version === STORAGE_VERSION ? persisted : initialTerminal) as TerminalState,
    },
  ),
);

export const visibleCharts = (s: Pick<TerminalState, "layout" | "charts">) => s.charts.slice(0, s.layout);

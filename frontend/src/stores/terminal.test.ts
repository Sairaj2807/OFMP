import { beforeEach, describe, expect, it } from "vitest";

import { SnapshotBus } from "@/lib/snapshots";
import { STORAGE_KEY, STORAGE_VERSION, initialTerminal, useTerminal, visibleCharts } from "./terminal";

describe("terminal store", () => {
  beforeEach(() => {
    localStorage.clear();
    useTerminal.setState({ ...initialTerminal });
  });

  it("layout controls visible charts and keeps focus on a visible one", () => {
    useTerminal.getState().setLayout(4);
    useTerminal.getState().setActiveChart("c4");
    expect(visibleCharts(useTerminal.getState()).map((c) => c.id)).toEqual(["c1", "c2", "c3", "c4"]);
    useTerminal.getState().setLayout(2);
    expect(useTerminal.getState().activeChartId).toBe("c1");
  });

  it("updates one chart only and persists versioned JSON", () => {
    useTerminal.getState().updateChart("c2", { interval: 900, showPoc: false });
    const [c1, c2] = useTerminal.getState().charts;
    expect(c1.interval).toBe(60);
    expect([c2.interval, c2.showPoc]).toEqual([900, false]);
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY)!);
    expect(saved.version).toBe(STORAGE_VERSION);
    expect(saved.state.charts[1].interval).toBe(900);
    expect("setLayout" in saved.state).toBe(false);           // functions are not stored
  });
});

describe("snapshot bus", () => {
  it("replays the latest snapshot to new subscribers and stops after unsubscribe", () => {
    const bus = new SnapshotBus();
    const got: number[] = [];
    bus.publish("c1", { ready: true, status: undefined, chart: { row_size: 1, interval_sec: 60, cvd_offset: 1, bars: [] } });
    const off = bus.subscribe("c1", (s) => got.push(s.chart!.cvd_offset));
    bus.publish("c1", { ready: true, chart: { row_size: 1, interval_sec: 60, cvd_offset: 2, bars: [] } });
    off();
    bus.publish("c1", { ready: true, chart: { row_size: 1, interval_sec: 60, cvd_offset: 3, bars: [] } });
    expect(got).toEqual([1, 2]);
    bus.forget("c1");
    expect(bus.get("c1")).toBeUndefined();
  });
});

describe("config migration", () => {
  it("upgrades a v1 layout without losing settings", async () => {
    const { migrateConfig } = await import("./terminal");
    const v1 = { layout: 4, activeChartId: "c3", units: "lots",
                 charts: [{ id: "c1", interval: 300, ppr: 2, cellStyle: "ladder", displayMode: "delta",
                            showPoc: false, showValueArea: true, showImbalances: true }] };
    const out = migrateConfig(v1);
    expect([out.layout, out.activeChartId, out.units]).toEqual([4, "c3", "lots"]);
    expect(out.charts[0]).toMatchObject({ interval: 300, ppr: 2, cellStyle: "ladder", displayMode: "delta",
                                          showPoc: false, mode: "live", replayDate: null,
                                          view: "footprint", profileRow: 5 });
    expect(out.charts).toHaveLength(4);                      // missing charts filled from defaults
  });

  it("repairs invalid values field by field and never crashes on junk", async () => {
    const { migrateConfig, initialTerminal } = await import("./terminal");
    const out = migrateConfig({ layout: 3, units: "x", charts: [{ id: "c1", ppr: 99, mode: "replay", replayDate: "bad" }] });
    expect(out.layout).toBe(initialTerminal.layout);
    expect(out.units).toBe("qty");
    expect(out.charts[0]).toMatchObject({ ppr: 1, mode: "live", replayDate: null });
    expect(migrateConfig(null)).toEqual(initialTerminal);
    const v3 = migrateConfig({ charts: [{ id: "c2", view: "profile", profileRow: 20 }, { id: "c3", view: "pie", profileRow: 7 }] });
    expect(v3.charts.map((c) => [c.view, c.profileRow])).toEqual(
      [["footprint", 5], ["profile", 20], ["footprint", 5], ["footprint", 5]]);   // per chart; junk repaired
    expect(migrateConfig("garbage")).toEqual(initialTerminal);
  });
});

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

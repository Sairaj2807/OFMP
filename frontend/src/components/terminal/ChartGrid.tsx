"use client";

import { Group, Panel, Separator } from "react-resizable-panels";
import { useShallow } from "zustand/react/shallow";

import { useTerminal } from "@/stores/terminal";
import { ChartPanel } from "./ChartPanel";

/** 1, 2 (side by side) or 4 (2 x 2) resizable charts. */
export function ChartGrid() {
  const charts = useTerminal(useShallow((s) => s.charts.slice(0, s.layout)));

  if (charts.length === 1) return <ChartPanel config={charts[0]} />;
  if (charts.length === 2) {
    return (
      <Group orientation="horizontal" id="grid-2">
        <Panel id="g2-a" minSize="20"><ChartPanel config={charts[0]} /></Panel>
        <Separator className="w-px" />
        <Panel id="g2-b" minSize="20"><ChartPanel config={charts[1]} /></Panel>
      </Group>
    );
  }
  return (
    <Group orientation="vertical" id="grid-4">
      {[0, 2].map((row) => (
        <Panel key={row} id={`g4-row-${row}`} minSize="20">
          <Group orientation="horizontal" id={`grid-4-row-${row}`}>
            <Panel id={`g4-${row}-a`} minSize="20"><ChartPanel config={charts[row]} /></Panel>
            <Separator className="w-px" />
            <Panel id={`g4-${row}-b`} minSize="20"><ChartPanel config={charts[row + 1]} /></Panel>
          </Group>
        </Panel>
      )).flatMap((p, i) => (i === 0 ? [p] : [<Separator key={`sep-${i}`} className="h-px" />, p]))}
    </Group>
  );
}

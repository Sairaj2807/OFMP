// Latest snapshot per chart id, with listeners. Charts subscribe here and
// redraw imperatively, so a 2 Hz stream never re-renders the React tree.

import type { ChartSnapshot } from "./types";

type Listener = (snapshot: ChartSnapshot) => void;

export class SnapshotBus {
  private readonly latest = new Map<string, ChartSnapshot>();
  private readonly listeners = new Map<string, Set<Listener>>();

  publish(id: string, snapshot: ChartSnapshot): void {
    this.latest.set(id, snapshot);
    for (const fn of this.listeners.get(id) ?? []) fn(snapshot);
  }

  get(id: string): ChartSnapshot | undefined {
    return this.latest.get(id);
  }

  /** Calls `fn` with the latest snapshot right away (if any), then on every publish. */
  subscribe(id: string, fn: Listener): () => void {
    let set = this.listeners.get(id);
    if (!set) this.listeners.set(id, (set = new Set()));
    set.add(fn);
    const current = this.latest.get(id);
    if (current) fn(current);
    return () => {
      set.delete(fn);
    };
  }

  forget(id: string): void {
    this.latest.delete(id);
  }
}

export const snapshotBus = new SnapshotBus();

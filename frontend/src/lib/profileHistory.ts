// Latest historical profiles per chart id (the profile_history stream message),
// with listeners. Kept apart from the 2 Hz snapshot bus: history is static and
// arrives once per subscription, today's live profile arrives every push.
import type { ProfileHistory } from "./types";

type Listener = (history: ProfileHistory | undefined) => void;

export class HistoryBus {
  private readonly latest = new Map<string, ProfileHistory>();
  private readonly listeners = new Map<string, Set<Listener>>();

  publish(id: string, history: ProfileHistory | undefined): void {
    if (history) this.latest.set(id, history);
    else this.latest.delete(id);
    for (const fn of this.listeners.get(id) ?? []) fn(history);
  }

  get(id: string): ProfileHistory | undefined {
    return this.latest.get(id);
  }

  /** Calls `fn` with the current history right away, then on every change. */
  subscribe(id: string, fn: Listener): () => void {
    let set = this.listeners.get(id);
    if (!set) this.listeners.set(id, (set = new Set()));
    set.add(fn);
    fn(this.latest.get(id));
    return () => {
      set.delete(fn);
    };
  }
}

export const historyBus = new HistoryBus();

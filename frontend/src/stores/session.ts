// Signed-in user, live connection state and the active contract.
import { create } from "zustand";

import type { StreamStatus } from "@/lib/stream";
import type { Contract, MarketSession, User } from "@/lib/types";
import { DEFAULT_INTERVALS } from "./terminal";

export interface FeedState {
  connected: boolean;
  error: string | null;
  symbol: string | null;
  tickSize: number;
  lotSize: number;
}

interface SessionState {
  user: User | null;
  stream: StreamStatus;
  feed: FeedState;
  contracts: Contract[];
  intervals: number[];
  notice: string | null; // one transient message for the top bar
  market: MarketSession | null;
  setUser: (user: User | null) => void;
  setStream: (status: StreamStatus) => void;
  setFeed: (feed: Partial<FeedState>) => void;
  setContracts: (contracts: Contract[]) => void;
  setIntervals: (intervals: number[]) => void;
  setNotice: (notice: string | null) => void;
  setMarket: (market: MarketSession | null) => void;
}

export const useSession = create<SessionState>()((set) => ({
  user: null,
  stream: "idle",
  feed: { connected: false, error: null, symbol: null, tickSize: 0.1, lotSize: 65 },
  contracts: [],
  intervals: DEFAULT_INTERVALS,
  notice: null,
  market: null,
  setUser: (user) => set({ user }),
  setStream: (stream) => set({ stream }),
  setFeed: (feed) =>
    set((s) => {
      const next = { ...s.feed, ...feed };
      // snapshots arrive twice a second: only notify subscribers on real change
      return Object.entries(next).some(([k, v]) => s.feed[k as keyof FeedState] !== v) ? { feed: next } : s;
    }),
  setContracts: (contracts) => set({ contracts }),
  setIntervals: (intervals) => set({ intervals: intervals.length ? intervals : DEFAULT_INTERVALS }),
  setNotice: (notice) => set({ notice }),
  setMarket: (market) => set({ market }),
}));

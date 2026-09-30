"use client";

import { useEffect, useState } from "react";

import { price } from "@/lib/format";
import { snapshotBus } from "@/lib/snapshots";
import { useSession } from "@/stores/session";
import { useTerminal } from "@/stores/terminal";

/** Contracts of the configured underlying. The active (streaming) one shows live figures. */
export function Watchlist() {
  const contracts = useSession((s) => s.contracts);
  const symbol = useSession((s) => s.feed.symbol);
  const activeChartId = useTerminal((s) => s.activeChartId);
  const [quote, setQuote] = useState<{ ltp: number; change: number | null } | null>(null);

  useEffect(
    () =>
      snapshotBus.subscribe(activeChartId, (snap) => {
        const q = snap.quote;
        setQuote(q ? { ltp: q.ltp, change: q.close ? q.ltp - q.close : null } : null);
      }),
    [activeChartId],
  );

  return (
    <aside className="flex h-full flex-col bg-panel" aria-label="Watchlist">
      <h2 className="border-b border-line px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-muted">Watchlist</h2>
      <ul className="flex-1 overflow-y-auto">
        {contracts.length === 0 && <li className="px-3 py-2 text-muted">No contracts</li>}
        {contracts.map((c) => {
          const active = c.tradingsymbol === symbol;
          return (
            <li
              key={c.tradingsymbol}
              aria-current={active ? "true" : undefined}
              className={`border-b border-line/60 px-3 py-2 ${active ? "bg-raised" : ""}`}
            >
              <div className="flex items-baseline justify-between gap-2">
                <span className="num text-[12px] text-fg">{c.tradingsymbol}</span>
                {active && <span className="text-[10px] uppercase tracking-wide text-accent">live</span>}
              </div>
              <div className="flex items-baseline justify-between text-[11px] text-muted">
                <span>{c.expiry ? `exp ${c.expiry}` : ""}</span>
                {active && quote && (
                  <span className="num">
                    <span className="text-fg">{price(quote.ltp)}</span>{" "}
                    {quote.change != null && (
                      <span className={quote.change >= 0 ? "text-buy" : "text-sell"}>
                        {quote.change >= 0 ? "▲ +" : "▼ "}{quote.change.toFixed(2)}
                      </span>
                    )}
                  </span>
                )}
              </div>
            </li>
          );
        })}
      </ul>
    </aside>
  );
}

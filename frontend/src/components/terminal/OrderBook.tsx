"use client";

import { useEffect, useState } from "react";

import { compact, price } from "@/lib/format";
import { snapshotBus } from "@/lib/snapshots";
import type { ChartSnapshot } from "@/lib/types";
import { useSession } from "@/stores/session";
import { useTerminal } from "@/stores/terminal";

type Book = NonNullable<ChartSnapshot["book"]>;

/** Best-5 depth ladder: asks above (highest first), bids below, bars scaled to the largest level. */
export function OrderBook() {
  const activeChartId = useTerminal((s) => s.activeChartId);
  const units = useTerminal((s) => s.units);
  const lotSize = useSession((s) => s.feed.lotSize);
  const [book, setBook] = useState<Book | null>(null);

  useEffect(() => snapshotBus.subscribe(activeChartId, (snap) => setBook(snap.book ?? null)), [activeChartId]);

  const d = units === "lots" ? lotSize : 1;
  const levels = book ? [...book.asks.map((l) => ({ l, side: "ask" as const })), ...book.bids.map((l) => ({ l, side: "bid" as const }))] : [];
  const maxQty = Math.max(1, ...levels.map(({ l }) => l[1]));
  const asks = book ? [...book.asks].reverse() : [];

  const row = (side: "ask" | "bid", [p, qty, orders]: [number, number, number], key: string) => (
    <div key={key} className="relative grid grid-cols-[1fr_1fr_2.5rem] items-center px-3 py-[3px]">
      <span
        className={`absolute inset-y-0.5 right-0 ${side === "ask" ? "bg-sell-bg" : "bg-buy-bg"}`}
        style={{ width: `${(qty / maxQty) * 100}%` }}
        aria-hidden
      />
      <span className={`num relative ${side === "ask" ? "text-sell" : "text-buy"}`}>{price(p)}</span>
      <span className="num relative text-right text-fg">{compact(qty / d)}</span>
      <span className="num relative text-right text-muted">{orders}</span>
    </div>
  );

  return (
    <aside className="flex h-full flex-col bg-panel" aria-label="Order book">
      <h2 className="flex items-center justify-between border-b border-line px-3 py-2 text-[11px] font-semibold uppercase tracking-wider text-muted">
        <span>Order book</span>
        <span className="font-normal normal-case tracking-normal">{units === "lots" ? "lots" : "qty"} · orders</span>
      </h2>
      {!book ? (
        <p className="px-3 py-2 text-muted">No depth yet</p>
      ) : (
        <div className="text-[12px]">
          <div className="grid grid-cols-[1fr_1fr_2.5rem] px-3 py-1 text-[11px] text-muted">
            <span>Price</span><span className="text-right">Size</span><span className="text-right">#</span>
          </div>
          <div aria-label="Asks (sellers)">{asks.map((l, i) => row("ask", l, `a${i}`))}</div>
          <div className="my-1 flex justify-between border-y border-line bg-panel-2 px-3 py-1 text-[11px]">
            <span className="text-muted">Spread <span className="num text-fg-2">{price(book.spread)}</span></span>
            <span className="text-muted">Mid <span className="num text-fg-2">{price(book.mid)}</span></span>
          </div>
          <div aria-label="Bids (buyers)">{book.bids.map((l, i) => row("bid", l, `b${i}`))}</div>
          <div className="mt-2 px-3 text-[11px] text-muted">
            Book imbalance{" "}
            <span className={`num ${book.obi >= 0 ? "text-buy" : "text-sell"}`}>
              {book.obi >= 0 ? "+" : ""}{(book.obi * 100).toFixed(1)}%
            </span>{" "}
            <span className="text-muted">({book.obi >= 0 ? "bid-heavy" : "ask-heavy"})</span>
          </div>
        </div>
      )}
    </aside>
  );
}

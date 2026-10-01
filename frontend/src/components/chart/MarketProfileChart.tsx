"use client";

// One market profile chart, drawn on a canvas from the snapshot bus (the 2 Hz
// stream never re-renders React; only the stats strip does, on change).
//
// Draws: TPO letters per period (blocks when rows are too small for text),
// the value area band, TPO POC (solid) and volume POC (dashed), initial
// balance bracket, single prints and tails, volume at price split into buy /
// sell, HVN / LVN ticks, the previous session's POC / VAH / VAL, the price
// axis with the last price, and a hover readout.
import { useEffect, useRef, useState } from "react";

import { compact, price as fmtPrice, signed } from "@/lib/format";
import { snapshotBus } from "@/lib/snapshots";
import {
  type ProfileLayout, centreOn, computeLayout, labelEvery, letterIndex, periodColor, rowIndexOf,
} from "@/lib/chart/profileLayout";
import type { MarketProfile, ProfileRow } from "@/lib/types";
import type { ChartConfig, Units } from "@/stores/terminal";

const C = {
  bg: "#12151b", grid: "#181c23", axis: "#252b35", text: "#a3acba", strong: "#dfe4ec", muted: "#6f7888",
  buy: "#1fb58f", sell: "#e5485d", va: "rgba(76,141,255,0.10)", poc: "#e0a43a", vpoc: "#dfe4ec",
  ib: "#4c8dff", single: "#e0a43a", prev: "#8a93a3", hvn: "#1fb58f", lvn: "#e5485d",
};

interface Props {
  config: ChartConfig;
  units: Units;
  lotSize: number;
}

export function MarketProfileChart({ config, units, lotSize }: Props) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const tipRef = useRef<HTMLDivElement>(null);
  const profileRef = useRef<MarketProfile | null>(null);
  const scrollRef = useRef<number | null>(null);           // null: follow the last price
  const optsRef = useRef({ showPoc: config.showPoc, showValueArea: config.showValueArea, units, lotSize });
  const drawRef = useRef<() => void>(() => undefined);
  const [summary, setSummary] = useState<MarketProfile | null>(null);
  const { id } = config;

  useEffect(() => {
    optsRef.current = { showPoc: config.showPoc, showValueArea: config.showValueArea, units, lotSize };
    drawRef.current();
  }, [config.showPoc, config.showValueArea, units, lotSize]);

  useEffect(() => {
    const canvas = canvasRef.current;
    const wrap = wrapRef.current;
    if (!canvas || !wrap) return;
    let layout: ProfileLayout | null = null;

    const draw = () => {
      const p = profileRef.current;
      const ctx = canvas.getContext("2d");
      if (!ctx) return;
      const dpr = window.devicePixelRatio || 1;
      const w = wrap.clientWidth, h = wrap.clientHeight;
      if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
        canvas.width = Math.round(w * dpr);
        canvas.height = Math.round(h * dpr);
        canvas.style.width = `${w}px`;
        canvas.style.height = `${h}px`;
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.fillStyle = C.bg;
      ctx.fillRect(0, 0, w, h);
      if (!p || !p.rows.length || !p.row_size) {
        layout = null;
        return;
      }
      layout = computeLayout(p, w, h);
      drawProfile(ctx, p, layout, scrollFor(p, layout), optsRef.current);
    };

    const scrollFor = (p: MarketProfile, l: ProfileLayout) => {
      if (scrollRef.current !== null) return Math.min(scrollRef.current, l.maxScroll);
      const close = p.stats?.close;
      return close == null ? 0 : centreOn(rowIndexOf(p.rows, close, p.row_size!), l);
    };

    drawRef.current = draw;
    const unsubscribe = snapshotBus.subscribe(id, (snap) => {
      const prof = snap.profile ?? null;
      profileRef.current = prof;
      setSummary((prev) => (sameSummary(prev, prof) ? prev : prof));
      draw();
    });
    const resize = new ResizeObserver(() => draw());
    resize.observe(wrap);

    const onWheel = (e: WheelEvent) => {
      const p = profileRef.current;
      if (!p || !layout || !layout.maxScroll) return;
      e.preventDefault();
      const cur = scrollRef.current ?? scrollFor(p, layout);
      scrollRef.current = Math.max(0, Math.min(layout.maxScroll, cur + Math.sign(e.deltaY) * 3));
      draw();
    };
    const onDouble = () => {
      scrollRef.current = null;                          // back to following the last price
      draw();
    };
    const onMove = (e: MouseEvent) => {
      const tip = tipRef.current, p = profileRef.current;
      if (!tip || !p || !layout) return;
      const rect = canvas.getBoundingClientRect();
      const y = e.clientY - rect.top;
      const row = y < layout.top ? undefined : p.rows[Math.floor((y - layout.top) / layout.rowPx) + scrollFor(p, layout)];
      if (!row) {
        tip.hidden = true;
        return;
      }
      const d = optsRef.current.units === "lots" ? optsRef.current.lotSize : 1;
      tip.hidden = false;
      tip.textContent = `${fmtPrice(row.price)}  ${row.letters || "–"}  TPO ${row.tpo}  ·  vol ${compact(row.volume / d)}`
        + `  (buy ${compact(row.buy / d)} / sell ${compact(row.sell / d)}, Δ ${signed(row.delta / d)})`;
      tip.style.top = `${Math.min(y + 12, rect.height - 24)}px`;
    };
    const onLeave = () => {
      if (tipRef.current) tipRef.current.hidden = true;
    };
    canvas.addEventListener("wheel", onWheel, { passive: false });
    canvas.addEventListener("dblclick", onDouble);
    canvas.addEventListener("mousemove", onMove);
    canvas.addEventListener("mouseleave", onLeave);
    return () => {
      unsubscribe();
      resize.disconnect();
      canvas.removeEventListener("wheel", onWheel);
      canvas.removeEventListener("dblclick", onDouble);
      canvas.removeEventListener("mousemove", onMove);
      canvas.removeEventListener("mouseleave", onLeave);
    };
  }, [id]);

  useEffect(() => {
    scrollRef.current = null;
    drawRef.current();
  }, [config.profileRow]);

  const d = units === "lots" ? lotSize : 1;
  const s = summary;
  return (
    <div className="flex h-full flex-col" data-testid={`profile-${id}`}>
      <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-0.5 border-b border-line px-2 py-1 text-[11px] text-muted"
           aria-label={`Market profile figures ${id}`}>
        {s?.stats ? (
          <>
            <Fig label="POC" value={fmtPrice(s.poc_tpo)} title="TPO point of control (most time at price)" />
            <Fig label="vPOC" value={fmtPrice(s.poc_volume)} title="Volume point of control" />
            <Fig label="VAH" value={fmtPrice(s.value_area_tpo?.high)} />
            <Fig label="VAL" value={fmtPrice(s.value_area_tpo?.low)} />
            <Fig label="IB" value={s.initial_balance?.high != null
              ? `${fmtPrice(s.initial_balance.low)}–${fmtPrice(s.initial_balance.high)}${s.initial_balance.final ? "" : " (forming)"}`
              : "—"} />
            <Fig label="TPOs" value={String(s.stats.tpo_total)} />
            <Fig label="Vol" value={compact(s.stats.volume / d)} />
            <Fig label="Δ" value={signed(s.stats.delta / d)} tone={s.stats.delta >= 0 ? "buy" : "sell"} />
            {!!s.single_prints?.length && <Fig label="Singles" value={String(s.single_prints.length)} />}
            {s.previous && <Fig label="Prev POC" value={fmtPrice(s.previous.poc_tpo)} title={`Session ${s.previous.date}`} />}
          </>
        ) : (
          <span>{s?.previous ? `No trades this session yet · previous POC ${fmtPrice(s.previous.poc_tpo)}` : "Waiting for the first trade…"}</span>
        )}
      </div>
      <div ref={wrapRef} className="relative min-h-0 flex-1">
        <canvas ref={canvasRef} className="absolute inset-0" aria-label={`Market profile chart ${id}`} role="img" />
        <div ref={tipRef} hidden
             className="num pointer-events-none absolute left-2 z-10 rounded border border-line-strong bg-panel-2 px-2 py-0.5 text-[11px] text-fg" />
      </div>
    </div>
  );
}

function Fig({ label, value, title, tone }: { label: string; value: string; title?: string; tone?: "buy" | "sell" }) {
  return (
    <span title={title}>
      {label}{" "}
      <span className={`num ${tone === "buy" ? "text-buy" : tone === "sell" ? "text-sell" : "text-fg"}`}>{value}</span>
    </span>
  );
}

function sameSummary(a: MarketProfile | null, b: MarketProfile | null): boolean {
  if (a === b) return true;
  if (!a || !b) return false;
  return a.poc_tpo === b.poc_tpo && a.poc_volume === b.poc_volume && a.stats?.tpo_total === b.stats?.tpo_total
    && a.stats?.volume === b.stats?.volume && a.value_area_tpo?.high === b.value_area_tpo?.high
    && a.value_area_tpo?.low === b.value_area_tpo?.low && a.initial_balance?.final === b.initial_balance?.final
    && a.previous?.date === b.previous?.date && a.single_prints?.length === b.single_prints?.length;
}

function drawProfile(ctx: CanvasRenderingContext2D, p: MarketProfile, l: ProfileLayout, scroll: number,
                     opts: { showPoc: boolean; showValueArea: boolean }) {
  const rows = p.rows, rowSize = p.row_size!;
  const first = scroll, last = Math.min(rows.length, scroll + l.visibleRows + 1);
  const yOf = (i: number) => l.top + (i - scroll) * l.rowPx;
  const yOfPrice = (price: number | null | undefined) => {
    if (price == null) return null;
    const i = rowIndexOf(rows, price, rowSize);
    return i < 0 ? null : yOf(i) + l.rowPx / 2;
  };
  const rowRange = (lo: number, hi: number) => {          // [top y, bottom y] of rows covering lo..hi
    const top = rowIndexOf(rows, hi, rowSize), bottom = rowIndexOf(rows, lo, rowSize);
    return top < 0 || bottom < 0 ? null : [yOf(top), yOf(bottom) + l.rowPx] as const;
  };

  // value area band
  if (opts.showValueArea && p.value_area_tpo) {
    const r = rowRange(p.value_area_tpo.low, p.value_area_tpo.high);
    if (r) {
      ctx.fillStyle = C.va;
      ctx.fillRect(0, r[0], l.axisX, r[1] - r[0]);
    }
  }

  // TPO POC: the row highlighted behind its letters (a line would strike through them)
  const pocY = opts.showPoc ? yOfPrice(p.poc_tpo) : null;
  if (pocY != null) {
    ctx.fillStyle = "rgba(224,164,58,0.22)";
    ctx.fillRect(l.letterX - 2, pocY - l.rowPx / 2, l.letterW, l.rowPx);
    ctx.fillStyle = C.poc;
    ctx.fillRect(l.letterX - 4, pocY - l.rowPx / 2, 2, l.rowPx);
  }

  // rows: TPOs and volume
  const current = p.current_period ?? -1;
  ctx.textBaseline = "middle";
  ctx.font = `${l.fontPx}px ui-monospace, Consolas, monospace`;
  for (let i = first; i < last; i++) {
    const row: ProfileRow = rows[i];
    const y = yOf(i);
    for (let k = 0; k < row.letters.length; k++) {
      const ch = row.letters[k];
      const idx = letterIndex(ch);
      const x = l.letterX + k * l.cellW;
      ctx.fillStyle = periodColor(idx);
      ctx.globalAlpha = idx === current ? 1 : 0.85;
      if (l.drawLetters) {
        ctx.fillText(ch, x + 1, y + l.rowPx / 2);
      } else {
        ctx.fillRect(x, y + 1, Math.max(1, l.cellW - 1), Math.max(1, l.rowPx - 2));
      }
    }
    ctx.globalAlpha = 1;
    if (l.maxVolume > 0 && row.volume > 0) {
      const total = (row.volume / l.maxVolume) * l.volumeW;
      const buyW = (row.buy / row.volume) * total;
      const h = Math.max(1, l.rowPx - 2);
      ctx.fillStyle = C.buy;
      ctx.globalAlpha = 0.75;
      ctx.fillRect(l.volumeX, y + 1, buyW, h);
      ctx.fillStyle = C.sell;
      ctx.fillRect(l.volumeX + buyW, y + 1, total - buyW, h);
      ctx.globalAlpha = 1;
    }
  }

  // HVN / LVN ticks at the volume area's left edge
  for (const [prices, color] of [[p.hvn ?? [], C.hvn], [p.lvn ?? [], C.lvn]] as const) {
    ctx.fillStyle = color;
    for (const price of prices) {
      const y = yOfPrice(price);
      if (y != null) ctx.fillRect(l.volumeX - 5, y - 1, 3, 2);
    }
  }

  // single prints and tails: a bar at the left edge
  for (const sp of [...(p.single_prints ?? []), ...(p.tails ?? [])]) {
    const r = rowRange(sp.low, sp.high);
    if (!r) continue;
    ctx.fillStyle = "kind" in sp ? C.muted : C.single;
    ctx.fillRect(6, r[0] + 1, 2, r[1] - r[0] - 2);
  }

  // initial balance bracket
  const ib = p.initial_balance;
  if (ib?.high != null && ib.low != null) {
    const r = rowRange(ib.low, ib.high);
    if (r) {
      ctx.strokeStyle = C.ib;
      ctx.setLineDash(ib.final ? [] : [3, 3]);
      ctx.beginPath();
      ctx.moveTo(4, r[0] + 1);
      ctx.lineTo(2, r[0] + 1);
      ctx.lineTo(2, r[1] - 1);
      ctx.lineTo(4, r[1] - 1);
      ctx.stroke();
      ctx.setLineDash([]);
    }
  }

  // volume POC: its bar outlined
  const vpocY = opts.showPoc ? yOfPrice(p.poc_volume) : null;
  if (vpocY != null) {
    ctx.strokeStyle = C.vpoc;
    ctx.lineWidth = 1;
    ctx.strokeRect(l.volumeX - 0.5, Math.round(vpocY - l.rowPx / 2) + 0.5, l.volumeW + 1, Math.max(2, l.rowPx - 1));
  }

  // previous session reference levels
  if (p.previous) {
    ctx.font = "10px ui-sans-serif, system-ui, sans-serif";
    for (const [label, value] of [["pPOC", p.previous.poc_tpo], ["pVAH", p.previous.vah], ["pVAL", p.previous.val]] as const) {
      const y = previousY(rows, value, rowSize, yOf, l);
      if (y == null) continue;
      hline(ctx, y, 0, l.axisX, C.prev, [1, 3]);
      ctx.fillStyle = C.prev;
      ctx.fillText(label, l.axisX - 30, y - 6);
    }
  }

  // price axis
  ctx.fillStyle = C.bg;
  ctx.fillRect(l.axisX, 0, l.width - l.axisX, l.height);
  ctx.strokeStyle = C.axis;
  ctx.beginPath();
  ctx.moveTo(l.axisX + 0.5, 0);
  ctx.lineTo(l.axisX + 0.5, l.height);
  ctx.stroke();
  ctx.font = "11px ui-monospace, Consolas, monospace";
  ctx.fillStyle = C.text;
  const every = labelEvery(l.rowPx);
  const digits = rowSize < 1 ? 2 : 0;
  const close = p.stats?.close;
  const cy = yOfPrice(close);
  for (let i = first; i < last; i++) {
    const y = yOf(i) + l.rowPx / 2;
    if (i % every || (cy != null && Math.abs(y - cy) < 14)) continue;     // keep clear of the last-price box
    ctx.fillText(rows[i].price.toFixed(digits), l.axisX + 6, y);
  }
  if (close != null && cy != null) {
    ctx.fillStyle = C.strong;
    ctx.fillRect(l.axisX + 1, cy - 8, l.width - l.axisX - 1, 16);
    ctx.fillStyle = C.bg;
    ctx.fillText(close.toFixed(2), l.axisX + 6, cy);
  }
}

/** y of a previous-session level: inside today's rows, or pinned to the edge (with the level beyond it). */
function previousY(rows: ProfileRow[], value: number | null, rowSize: number, yOf: (i: number) => number,
                   l: ProfileLayout): number | null {
  if (value == null) return null;
  const i = rowIndexOf(rows, value, rowSize);
  if (i >= 0) return yOf(i) + l.rowPx / 2;
  const top = rows[0].price;
  const offset = Math.round((top - value) / rowSize);
  const y = yOf(offset) + l.rowPx / 2;
  return y >= 0 && y <= l.height ? y : null;
}

function hline(ctx: CanvasRenderingContext2D, y: number | null, x0: number, x1: number, color: string, dash: number[]) {
  if (y == null) return;
  ctx.strokeStyle = color;
  ctx.setLineDash(dash);
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(x0, Math.round(y) + 0.5);
  ctx.lineTo(x1, Math.round(y) + 0.5);
  ctx.stroke();
  ctx.setLineDash([]);
}

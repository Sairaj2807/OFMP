"use client";

// Multi-day market profile: finished sessions (static, from the profile_history
// message) and today (live, from the 2 Hz snapshot) side by side, every day an
// independent profile, all on ONE absolute price axis (lib/chart/multiDay.ts).
//
// Data -> columns (collectColumns) -> price grid (priceGrid) -> layout
// (computeMultiLayout) -> mapper (priceToY / sessionToX) -> drawSession per
// visible column -> header and price axis. Hit testing uses the same mapper:
// session first, then price, then that session's own row.
import { useEffect, useRef, useState } from "react";

import { periodColor } from "@/lib/chart/profileLayout";
import {
  clampScroll, collectColumns, computeMultiLayout, type DayColumn, followScroll, mapper as makeMapper, type Mapper,
  type MultiLayout, type PriceGrid, priceGrid, rowAt, type Scroll, shortDate,
} from "@/lib/chart/multiDay";
import { compact, price as fmtPrice, signed } from "@/lib/format";
import { historyBus } from "@/lib/profileHistory";
import { snapshotBus } from "@/lib/snapshots";
import type { MarketProfile, ProfileHistory } from "@/lib/types";
import type { ChartConfig, Units } from "@/stores/terminal";

const C = {
  bg: "#12151b", grid: "#181c23", axis: "#252b35", text: "#a3acba", strong: "#dfe4ec", muted: "#6f7888",
  buy: "#1fb58f", sell: "#e5485d", va: "rgba(76,141,255,0.10)", poc: "#e0a43a", vpoc: "#dfe4ec",
  ib: "#4c8dff", single: "#e0a43a", hvn: "#1fb58f", lvn: "#e5485d", live: "#4c8dff", liveBg: "rgba(76,141,255,0.05)",
};

interface Props {
  config: ChartConfig;
  units: Units;
  lotSize: number;
}

interface Focus {
  column: DayColumn;
  excluded: number;
  excludedTitle: string;
}

export function MultiDayProfileChart({ config, units, lotSize }: Props) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const tipRef = useRef<HTMLDivElement>(null);
  const liveRef = useRef<MarketProfile | null>(null);
  const historyRef = useRef<ProfileHistory | undefined>(undefined);
  const scrollRef = useRef<Scroll | null>(null);            // null: follow today
  const hoverRef = useRef<number | null>(null);             // hovered column index
  const optsRef = useRef({ showPoc: config.showPoc, showValueArea: config.showValueArea, mode: config.profileMode,
                           units, lotSize });
  const drawRef = useRef<() => void>(() => undefined);
  const [focus, setFocus] = useState<Focus | null>(null);
  const { id } = config;

  useEffect(() => {
    optsRef.current = { showPoc: config.showPoc, showValueArea: config.showValueArea, mode: config.profileMode,
                        units, lotSize };
    drawRef.current();
  }, [config.showPoc, config.showValueArea, config.profileMode, units, lotSize]);

  useEffect(() => {
    const canvas = canvasRef.current, wrap = wrapRef.current;
    if (!canvas || !wrap) return;
    let view: { columns: DayColumn[]; grid: PriceGrid; layout: MultiLayout; m: Mapper } | null = null;
    let lastFocusKey = "";

    const publishFocus = (columns: DayColumn[]) => {
      const i = hoverRef.current ?? columns.length - 1;
      const column = columns[Math.min(i, columns.length - 1)];
      const h = historyRef.current;
      const excl = h?.excluded ?? [];
      const key = `${column?.date}|${column?.state}|${column?.profile?.stats?.tpo_total}|${column?.profile?.stats?.volume}`
        + `|${column?.profile?.poc_tpo}|${excl.length}`;
      if (key === lastFocusKey) return;
      lastFocusKey = key;
      setFocus(column ? { column, excluded: excl.length,
                          excludedTitle: excl.map((e) => `${e.date}: ${e.reason}`).join("\n") } : null);
    };

    const draw = () => {
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
      const columns = collectColumns(historyRef.current, liveRef.current);
      const rowSize = liveRef.current?.row_size ?? historyRef.current?.row_size ?? config.profileRow;
      const grid = priceGrid(columns, rowSize);
      publishFocus(columns);
      if (!grid) {
        view = null;
        return;
      }
      const opts = optsRef.current;
      const layout = computeMultiLayout(columns, grid, w, h, opts.mode);
      const scroll = scrollRef.current ? clampScroll(scrollRef.current, layout)
        : followScroll(layout, grid, liveRef.current?.stats?.close);
      const m = makeMapper(layout, grid, scroll, columns.length);
      view = { columns, grid, layout, m };
      const [first, last] = m.visibleSessions();
      ctx.save();
      ctx.beginPath();
      ctx.rect(0, layout.plotTop, layout.axisX, layout.plotH + layout.footer);
      ctx.clip();
      for (let i = first; i <= last; i++) drawSession(ctx, columns[i], i, m, layout, grid, opts);
      ctx.restore();
      drawHeader(ctx, columns, first, last, m, layout);
      drawAxis(ctx, m, layout, grid, liveRef.current?.stats?.close ?? null);
    };
    drawRef.current = draw;

    const offLive = snapshotBus.subscribe(id, (snap) => {
      liveRef.current = snap.profile ?? null;
      draw();
    });
    const offHistory = historyBus.subscribe(id, (h) => {
      historyRef.current = h;
      draw();
    });
    const resize = new ResizeObserver(() => draw());
    resize.observe(wrap);

    const scrollBy = (dx: number, dy: number) => {
      if (!view) return;
      const cur = scrollRef.current ?? followScroll(view.layout, view.grid, liveRef.current?.stats?.close);
      scrollRef.current = clampScroll({ x: cur.x + dx, y: cur.y + dy }, view.layout);
      draw();
    };
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      if (e.shiftKey) scrollBy(e.deltaY, 0);                   // shift + wheel: sessions
      else scrollBy(e.deltaX, Math.sign(e.deltaY) * 3);         // wheel: price; trackpad sideways: sessions
    };
    let drag: { x: number; y: number; acc: number } | null = null;
    const onDown = (e: PointerEvent) => {
      drag = { x: e.clientX, y: e.clientY, acc: 0 };
      canvas.setPointerCapture(e.pointerId);
    };
    const onUp = (e: PointerEvent) => {
      drag = null;
      if (canvas.hasPointerCapture(e.pointerId)) canvas.releasePointerCapture(e.pointerId);
    };
    const onMove = (e: MouseEvent) => {
      if (drag && view) {
        const dx = drag.x - e.clientX;
        drag.acc += e.clientY - drag.y;
        const rows = Math.trunc(drag.acc / view.layout.rowPx);
        drag.acc -= rows * view.layout.rowPx;
        drag.x = e.clientX;
        drag.y = e.clientY;
        scrollBy(dx, -rows);
        return;
      }
      hover(e);
    };
    const hover = (e: MouseEvent) => {
      const tip = tipRef.current;
      if (!tip || !view) return;
      const rect = canvas.getBoundingClientRect();
      const x = e.clientX - rect.left, y = e.clientY - rect.top;
      const i = view.m.xToSession(x);
      const prev = hoverRef.current;
      hoverRef.current = i;
      if (prev !== i) publishFocus(view.columns);
      const priceAt = view.m.yToPrice(y);
      const col = i === null ? null : view.columns[i];
      const row = col && priceAt !== null ? rowAt(col, priceAt, view.grid.rowSize) : null;
      if (!col || !row) {
        tip.hidden = true;
        return;
      }
      const d = optsRef.current.units === "lots" ? optsRef.current.lotSize : 1;
      tip.hidden = false;
      tip.textContent = `${col.state === "live" ? "Today" : shortDate(col.date)} · ${fmtPrice(row.price)}  `
        + `${row.letters || "–"}  TPO ${row.tpo} · vol ${compact(row.volume / d)} (buy ${compact(row.buy / d)} / `
        + `sell ${compact(row.sell / d)}, Δ ${signed(row.delta / d)})`;
      tip.style.top = `${Math.min(y + 12, rect.height - 24)}px`;
    };
    const onLeave = () => {
      if (tipRef.current) tipRef.current.hidden = true;
      hoverRef.current = null;
      if (view) publishFocus(view.columns);
    };
    const onDouble = () => {
      scrollRef.current = null;                                // follow today again
      draw();
    };
    canvas.addEventListener("wheel", onWheel, { passive: false });
    canvas.addEventListener("pointerdown", onDown);
    canvas.addEventListener("pointerup", onUp);
    canvas.addEventListener("mousemove", onMove);
    canvas.addEventListener("mouseleave", onLeave);
    canvas.addEventListener("dblclick", onDouble);
    return () => {
      offLive();
      offHistory();
      resize.disconnect();
      canvas.removeEventListener("wheel", onWheel);
      canvas.removeEventListener("pointerdown", onDown);
      canvas.removeEventListener("pointerup", onUp);
      canvas.removeEventListener("mousemove", onMove);
      canvas.removeEventListener("mouseleave", onLeave);
      canvas.removeEventListener("dblclick", onDouble);
    };
  }, [id, config.profileRow]);

  useEffect(() => {
    scrollRef.current = null;
    drawRef.current();
  }, [config.profileRow, config.profileDays]);

  const d = units === "lots" ? lotSize : 1;
  const col = focus?.column;
  const p = col?.profile;
  const label = !col ? "" : col.state === "live" ? `Today ${col.date !== "today" ? shortDate(col.date) : ""}` : shortDate(col.date);
  return (
    <div className="flex h-full flex-col" data-testid={`profile-${id}`}>
      <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-0.5 border-b border-line px-2 py-1 text-[11px] text-muted"
           aria-label={`Market profile figures ${id}`}>
        {col && <span className="font-semibold text-fg" aria-label="Focused session">{label}</span>}
        {p?.stats ? (
          <>
            <Fig label="POC" value={fmtPrice(p.poc_tpo)} />
            <Fig label="vPOC" value={fmtPrice(p.poc_volume)} />
            <Fig label="VAH" value={fmtPrice(p.value_area_tpo?.high)} />
            <Fig label="VAL" value={fmtPrice(p.value_area_tpo?.low)} />
            <Fig label="IB" value={p.initial_balance?.high != null
              ? `${fmtPrice(p.initial_balance.low)}–${fmtPrice(p.initial_balance.high)}${p.initial_balance.final ? "" : " (forming)"}`
              : "—"} />
            <Fig label="TPOs" value={String(p.stats.tpo_total)} />
            <Fig label="Vol" value={compact(p.stats.volume / d)} />
            <Fig label="Δ" value={signed(p.stats.delta / d)} tone={p.stats.delta >= 0 ? "buy" : "sell"} />
          </>
        ) : (
          <span>{col?.state === "error" ? `Error: ${col.error}` : "Waiting for the first trade…"}</span>
        )}
        {!!focus?.excluded && (
          <span className="ml-auto" title={focus.excludedTitle}>{focus.excluded} session(s) not shown</span>
        )}
      </div>
      <div ref={wrapRef} className="relative min-h-0 flex-1">
        <canvas ref={canvasRef} className="absolute inset-0 cursor-grab" role="img"
                aria-label={`Multi-day market profile chart ${id} (${config.profileMode})`} />
        <div ref={tipRef} hidden
             className="num pointer-events-none absolute left-2 z-10 rounded border border-line-strong bg-panel-2 px-2 py-0.5 text-[11px] text-fg" />
      </div>
    </div>
  );
}

function Fig({ label, value, tone }: { label: string; value: string; tone?: "buy" | "sell" }) {
  return (
    <span>
      {label}{" "}
      <span className={`num ${tone === "buy" ? "text-buy" : tone === "sell" ? "text-sell" : "text-fg"}`}>{value}</span>
    </span>
  );
}

// -- drawing primitives -------------------------------------------------------------------

/** One session's profile inside its own column. Nothing here reaches another column. */
function drawSession(ctx: CanvasRenderingContext2D, col: DayColumn, i: number, m: Mapper, l: MultiLayout,
                     grid: PriceGrid, opts: { showPoc: boolean; showValueArea: boolean; mode: string }) {
  const x0 = m.sessionToX(i);
  const tpoX0 = x0 + 14, volX = tpoX0 + l.tpoW + 6;
  const bottom = l.plotTop + l.plotH;
  if (col.state === "live") {
    ctx.fillStyle = C.liveBg;
    ctx.fillRect(x0, l.plotTop, l.colW, l.plotH + l.footer);
  }
  // session separator (left edge)
  ctx.strokeStyle = C.axis;
  ctx.beginPath();
  ctx.moveTo(Math.round(x0) + 0.5, l.plotTop);
  ctx.lineTo(Math.round(x0) + 0.5, bottom + l.footer);
  ctx.stroke();

  const p = col.profile;
  if (!p || !p.rows.length) {
    ctx.fillStyle = C.muted;
    ctx.font = "11px ui-sans-serif, system-ui, sans-serif";
    ctx.fillText(col.state === "error" ? "Could not load" : "No trades yet", tpoX0, l.plotTop + 16);
    return;
  }
  const rowY = (price: number) => m.priceToY(price);
  const band = (lo: number, hi: number) => [rowY(hi), rowY(lo) + l.rowPx] as const;
  const visible = (y: number) => y + l.rowPx >= l.plotTop && y <= bottom;

  if (opts.showValueArea && p.value_area_tpo) {
    const [t, b] = band(p.value_area_tpo.low, p.value_area_tpo.high);
    ctx.fillStyle = C.va;
    ctx.fillRect(tpoX0 - 2, t, l.tpoW + 6 + l.volumeW, b - t);
  }
  if (opts.showPoc && p.poc_tpo != null) {
    const y = rowY(p.poc_tpo);
    ctx.fillStyle = "rgba(224,164,58,0.22)";
    ctx.fillRect(tpoX0 - 2, y, l.tpoW + 2, l.rowPx);
    ctx.fillStyle = C.poc;
    ctx.fillRect(tpoX0 - 4, y, 2, l.rowPx);
  }

  const maxVol = p.rows.reduce((mx, r) => Math.max(mx, r.volume), 0);      // this day's own scale
  const current = col.state === "live" ? (p.current_period ?? -1) : -1;
  ctx.textBaseline = "middle";
  ctx.font = `${l.fontPx}px ui-monospace, Consolas, monospace`;
  for (const row of p.rows) {
    const y = rowY(row.price);
    if (!visible(y)) continue;
    for (let k = 0; k < row.letters.length; k++) {
      const ch = row.letters[k];
      const idx = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz".indexOf(ch);
      const x = m.tpoX(i, k, ch);
      ctx.fillStyle = periodColor(idx);
      ctx.globalAlpha = idx === current || current < 0 ? 0.95 : 0.8;
      if (l.drawLetters) ctx.fillText(ch, x + 1, y + l.rowPx / 2);
      else ctx.fillRect(x, y + 1, Math.max(1, l.cellW - 1), Math.max(1, l.rowPx - 2));
    }
    ctx.globalAlpha = 1;
    if (maxVol > 0 && row.volume > 0) {
      const total = (row.volume / maxVol) * l.volumeW, buyW = (row.buy / row.volume) * total;
      const h = Math.max(1, l.rowPx - 2);
      ctx.globalAlpha = 0.75;
      ctx.fillStyle = C.buy;
      ctx.fillRect(volX, y + 1, buyW, h);
      ctx.fillStyle = C.sell;
      ctx.fillRect(volX + buyW, y + 1, total - buyW, h);
      ctx.globalAlpha = 1;
    }
  }
  if (opts.showPoc && p.poc_volume != null) {
    const y = rowY(p.poc_volume);
    ctx.strokeStyle = C.vpoc;
    ctx.strokeRect(volX - 0.5, Math.round(y) + 0.5, l.volumeW + 1, Math.max(2, l.rowPx - 1));
  }
  for (const [prices, color] of [[p.hvn ?? [], C.hvn], [p.lvn ?? [], C.lvn]] as const) {
    ctx.fillStyle = color;
    for (const price of prices) ctx.fillRect(volX - 4, rowY(price) + l.rowPx / 2 - 1, 3, 2);
  }
  for (const sp of [...(p.single_prints ?? []), ...(p.tails ?? [])]) {
    const [t, b] = band(sp.low, sp.high);
    ctx.fillStyle = "kind" in sp ? C.muted : C.single;
    ctx.fillRect(x0 + 7, t + 1, 2, b - t - 2);
  }
  const ib = p.initial_balance;
  if (ib?.high != null && ib.low != null) {
    const [t, b] = band(ib.low, ib.high);
    ctx.strokeStyle = C.ib;
    ctx.setLineDash(ib.final ? [] : [3, 3]);
    ctx.beginPath();
    ctx.moveTo(x0 + 6, t + 1);
    ctx.lineTo(x0 + 4, t + 1);
    ctx.lineTo(x0 + 4, b - 1);
    ctx.lineTo(x0 + 6, b - 1);
    ctx.stroke();
    ctx.setLineDash([]);
  }
  if (l.mode === "split") {
    for (const per of p.periods ?? []) {
      const x = tpoX0 + per.index * l.cellW;
      ctx.fillStyle = periodColor(per.index);
      ctx.fillRect(x - 1, rowY(per.open) + 1, 2, Math.max(2, l.rowPx - 2));
      ctx.fillRect(x + l.cellW - 2, rowY(per.close) + 1, 2, Math.max(2, l.rowPx - 2));
      if (l.footer) {
        ctx.font = "9px ui-monospace, Consolas, monospace";
        ctx.fillText(per.letter, x + 2, bottom + l.footer / 2);
        ctx.font = `${l.fontPx}px ui-monospace, Consolas, monospace`;
      }
    }
  }
}

function drawHeader(ctx: CanvasRenderingContext2D, columns: DayColumn[], first: number, last: number, m: Mapper,
                    l: MultiLayout) {
  ctx.fillStyle = C.bg;
  ctx.fillRect(0, 0, l.width, l.plotTop);
  ctx.save();
  ctx.beginPath();
  ctx.rect(0, 0, l.axisX, l.plotTop);
  ctx.clip();
  ctx.textBaseline = "middle";
  for (let i = first; i <= last; i++) {
    const col = columns[i], x = m.sessionToX(i);
    const live = col.state === "live";
    ctx.fillStyle = live ? C.live : C.strong;
    ctx.font = "600 11px ui-sans-serif, system-ui, sans-serif";
    ctx.fillText(live ? "TODAY" : shortDate(col.date), x + 14, 12);
    ctx.fillStyle = C.muted;
    ctx.font = "10px ui-sans-serif, system-ui, sans-serif";
    const sub = col.state === "error" ? "error" : live ? (col.date !== "today" ? `${shortDate(col.date)} · live` : "live")
      : col.date.slice(0, 4);
    ctx.fillText(sub, x + 14, 25);
    ctx.strokeStyle = C.axis;
    ctx.beginPath();
    ctx.moveTo(Math.round(x) + 0.5, 0);
    ctx.lineTo(Math.round(x) + 0.5, l.plotTop);
    ctx.stroke();
  }
  ctx.restore();
  ctx.strokeStyle = C.axis;
  ctx.beginPath();
  ctx.moveTo(0, l.plotTop - 0.5);
  ctx.lineTo(l.axisX, l.plotTop - 0.5);
  ctx.stroke();
}

/** The one price axis (fixed while sessions scroll). The live last price is boxed here only. */
function drawAxis(ctx: CanvasRenderingContext2D, m: Mapper, l: MultiLayout, grid: PriceGrid, last: number | null) {
  ctx.fillStyle = C.bg;
  ctx.fillRect(l.axisX, 0, l.width - l.axisX, l.height);
  ctx.strokeStyle = C.axis;
  ctx.beginPath();
  ctx.moveTo(l.axisX + 0.5, 0);
  ctx.lineTo(l.axisX + 0.5, l.height);
  ctx.stroke();
  ctx.font = "11px ui-monospace, Consolas, monospace";
  ctx.textBaseline = "middle";
  const every = Math.max(1, Math.ceil(16 / l.rowPx));
  const digits = grid.rowSize < 1 ? 2 : 0;
  const lastY = last == null ? null : m.priceToY(last) + l.rowPx / 2;
  ctx.fillStyle = C.text;
  for (let row = grid.topRow; row >= grid.bottomRow; row--) {
    const price = row * grid.rowSize;
    const y = m.priceToY(price) + l.rowPx / 2;
    if (y < l.plotTop || y > l.plotTop + l.plotH) continue;
    if (row % every || (lastY != null && Math.abs(y - lastY) < 14)) continue;
    ctx.fillText(price.toFixed(digits), l.axisX + 6, y);
  }
  if (last != null && lastY != null && lastY >= l.plotTop && lastY <= l.plotTop + l.plotH) {
    ctx.fillStyle = C.strong;
    ctx.fillRect(l.axisX + 1, lastY - 8, l.width - l.axisX - 1, 16);
    ctx.fillStyle = C.bg;
    ctx.fillText(last.toFixed(2), l.axisX + 6, lastY);
  }
}

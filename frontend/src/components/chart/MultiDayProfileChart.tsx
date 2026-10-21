"use client";

// The market profile chart: one column (today, or the replayed session) or several
// (finished sessions + today), every day an independent profile on ONE absolute
// price axis, with pan and zoom on both axes (lib/chart/multiDay.ts for the model,
// hooks/useProfileViewport.ts for the gestures).
//
// Data -> columns -> price grid -> viewport -> layout -> mapper -> drawSession per
// visible column -> header and price axis. Hit testing uses the same mapper:
// session first, then price, then that session's own row. Redraws are coalesced
// into one per animation frame.
import { useEffect, useRef, useState } from "react";

import { periodColor } from "@/lib/chart/profileLayout";
import {
  LEVEL_COLORS, levelsOnRow, type LevelLine, nakedLabel, nakedToday, type ProfileLevel, profileDrawing,
} from "@/lib/chart/profileLevels";
import { api } from "@/lib/api";
import {
  clampViewport, collectColumns, computeLayout, type DayColumn, fitZoom, followView, labelStep, mapper as makeMapper,
  type Mapper, type MultiLayout, panBy, type PriceGrid, priceGrid, rowAt, shortDate, type Viewport, visibleSpan,
  zoomX, zoomY,
} from "@/lib/chart/multiDay";
import { compact, price as fmtPrice, signed } from "@/lib/format";
import { historyBus } from "@/lib/profileHistory";
import { snapshotBus } from "@/lib/snapshots";
import { type Region, useProfileViewport, type ViewportActions } from "@/hooks/useProfileViewport";
import type { MarketProfile, NakedPoc, ProfileHistory, ProfileLevelsResponse } from "@/lib/types";
import { type ChartConfig, type Units, useTerminal } from "@/stores/terminal";

const C = {
  bg: "#12151b", axis: "#252b35", text: "#a3acba", strong: "#dfe4ec", muted: "#6f7888",
  buy: "#1fb58f", sell: "#e5485d",
  ib: "#4c8dff", single: "#e0a43a", lvn: "#e5485d", live: "#4c8dff", liveBg: "rgba(76,141,255,0.05)",
  warn: "#e0a43a",       // level colours: lib/chart/profileLevels.ts
};
const LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";

interface Props {
  config: ChartConfig;
  units: Units;
  lotSize: number;
}

interface Focus {
  column: DayColumn;
  single: boolean;
  excluded: number;
  excludedTitle: string;
}

interface View {
  columns: DayColumn[];
  grid: PriceGrid;
  layout: MultiLayout;
  vp: Viewport;
  m: Mapper;
}

export function MultiDayProfileChart({ config, units, lotSize }: Props) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const tipRef = useRef<HTMLDivElement>(null);
  const liveRef = useRef<{ profile: MarketProfile | null; replay: boolean }>({ profile: null, replay: false });
  const historyRef = useRef<ProfileHistory | undefined>(undefined);
  const levelsRef = useRef<ProfileLevelsResponse | null>(null);     // naked POCs, composite (finished sessions)
  const vpRef = useRef<Viewport | null>(null);                   // null: not initialised yet
  const viewRef = useRef<View | null>(null);
  const anchorRef = useRef<{ date: string; offset: number } | null>(null);   // keeps a day in place when columns change
  const hoverRef = useRef<number | null>(null);
  const frameRef = useRef(0);
  const zoomSaveRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const settingsRef = useRef({ showPoc: config.showPoc, showValueArea: config.showValueArea, mode: config.profileMode,
                               units, lotSize, rowSize: config.profileRow, zoomY: config.profileZoomY,
                               zoomX: config.profileZoomX, levels: config.profileLevels as readonly ProfileLevel[] });
  const [focus, setFocus] = useState<Focus | null>(null);
  const [following, setFollowing] = useState(true);
  const { id } = config;
  const update = useTerminal((s) => s.updateChart);
  const multi = config.profileDays > 0 && config.mode === "live";
  const levelsKey = config.profileLevels.join(",");

  const schedule = () => {
    if (frameRef.current) return;
    frameRef.current = requestAnimationFrame(() => {
      frameRef.current = 0;
      draw();
    });
  };

  // -- viewport ---------------------------------------------------------------------------------

  const setVp = (vp: Viewport, persistZoom = false) => {
    const v = viewRef.current;
    vpRef.current = v ? clampViewport(vp, computeLayout(v.columns, v.layout.width, v.layout.height,
                                                          settingsRef.current.mode, vp), v.grid) : vp;
    setFollowing(vpRef.current.follow);
    if (tipRef.current) tipRef.current.hidden = true;     // the row under the cursor has changed
    if (persistZoom) {
      if (zoomSaveRef.current) clearTimeout(zoomSaveRef.current);
      const z = vpRef.current;
      zoomSaveRef.current = setTimeout(() => update(id, { profileZoomY: Math.round(z.rowPx * 100) / 100,
                                                          profileZoomX: Math.round(z.colScale * 1000) / 1000 }), 500);
    }
    schedule();
  };

  const draw = () => {
    const canvas = canvasRef.current, wrap = wrapRef.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !wrap || !ctx) return;
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
    const s = settingsRef.current;
    const live = liveRef.current;
    const columns = collectColumns(live.replay ? undefined : historyRef.current, live.profile, live.replay);
    const composite = live.replay ? null : levelsRef.current?.composite;
    if (composite?.rows?.length) columns.unshift({ date: "composite", state: "composite", profile: composite });
    const rowSize = live.profile?.row_size ?? historyRef.current?.row_size ?? s.rowSize;
    const grid = priceGrid(columns, rowSize);
    publishFocus(columns);
    if (!grid) {
      viewRef.current = null;
      return;
    }
    // initialise, or keep the view stable while data changes
    let vp = vpRef.current;
    if (!vp) {
      const fit = fitZoom(columns, grid, w, h, s.mode);
      vp = { topPrice: grid.high, leftX: 0, follow: true, rowPx: s.zoomY || fit.rowPx, colScale: s.zoomX || fit.colScale };
    }
    let layout = computeLayout(columns, w, h, s.mode, vp);
    if (vp.follow) {
      vp = followView(vp, layout, grid, live.profile?.stats?.close);
    } else {
      const anchor = anchorRef.current;
      const i = anchor ? columns.findIndex((c) => c.date === anchor.date) : -1;
      if (anchor && i >= 0) vp = { ...vp, leftX: i * layout.colW + anchor.offset };   // same day stays at the left
      vp = clampViewport(vp, layout, grid);
    }
    vpRef.current = vp;
    layout = computeLayout(columns, w, h, s.mode, vp);
    const m = makeMapper(layout, grid, vp, columns.length);
    viewRef.current = { columns, grid, layout, vp, m };
    const firstIdx = Math.max(0, Math.min(columns.length - 1, Math.floor(vp.leftX / layout.colW)));
    anchorRef.current = { date: columns[firstIdx].date, offset: vp.leftX - firstIdx * layout.colW };

    const [first, last] = m.visibleSessions();
    ctx.save();
    ctx.beginPath();
    ctx.rect(0, layout.plotTop, layout.axisX, layout.plotH + layout.footer);
    ctx.clip();
    for (let i = first; i <= last; i++) drawSession(ctx, columns[i], i, m, layout, s, columns.length === 1, grid.rowSize);
    if (!live.replay && s.levels.includes("npoc") && levelsRef.current) {
      drawNaked(ctx, nakedToday(levelsRef.current.naked_pocs, live.profile, grid.rowSize), columns, m, layout);
    }
    ctx.restore();
    drawHeader(ctx, columns, first, last, m, layout);
    drawAxis(ctx, m, layout, grid, live.profile?.stats?.close ?? null);
  };

  const publishFocusRef = useRef("");
  const publishFocus = (columns: DayColumn[]) => {
    const i = hoverRef.current ?? columns.length - 1;
    const column = columns[Math.min(i, columns.length - 1)];
    const excl = liveRef.current.replay ? [] : (historyRef.current?.excluded ?? []);
    const p = column?.profile;
    const key = [column?.date, column?.state, p?.stats?.tpo_total, p?.stats?.volume, p?.poc_tpo, p?.value_area_tpo?.high,
                 p?.initial_balance?.final, excl.length, columns.length].join("|");
    if (key === publishFocusRef.current) return;
    publishFocusRef.current = key;
    setFocus(column ? { column, single: columns.length === 1, excluded: excl.length,
                        excludedTitle: excl.map((e) => `${e.date}: ${e.reason}`).join("\n") } : null);
  };

  // -- gestures --------------------------------------------------------------------------------

  const withView = <T,>(fn: (v: View) => T): T | undefined => (viewRef.current ? fn(viewRef.current) : undefined);
  const actions: ViewportActions = {
    region: (x, y): Region => withView((v) => (x >= v.layout.axisX ? "axis" : y < v.layout.plotTop ? "header" : "plot"))
      ?? "plot",
    pan: (dx, dy) => withView((v) => setVp(panBy(v.vp, dx, dy, v.layout, v.grid.rowSize))),
    zoomY: (f, y) => withView((v) => setVp(zoomY(v.vp, f, y, v.layout, v.grid.rowSize), true)),
    zoomX: (f, x) => withView((v) => setVp(zoomX(v.vp, f, Math.min(x, v.layout.axisX)), true)),
    zoomBoth: (f, x, y) => withView((v) => {
      const zy = zoomY(v.vp, f, y, v.layout, v.grid.rowSize);
      setVp(zoomX(zy, f, Math.min(x, v.layout.axisX)), true);
    }),
    centre: () => withView((v) => ({ x: v.layout.axisX / 2, y: v.layout.plotTop + v.layout.plotH / 2 }))
      ?? { x: 0, y: 0 },
    follow: () => withView((v) => setVp({ ...v.vp, follow: true })),
    fitAll: () => withView((v) => {                     // every day and the whole range on screen
      const fit = fitZoom(v.columns, v.grid, v.layout.width, v.layout.height, settingsRef.current.mode);
      const colScale = Math.max(0.35, Math.min(fit.colScale, v.layout.axisX / (v.columns.length * v.layout.baseColW)));
      const l = computeLayout(v.columns, v.layout.width, v.layout.height, settingsRef.current.mode,
                              { rowPx: fit.rowPx, colScale });
      const rowPx = Math.max(2, Math.min(fit.rowPx, l.plotH / v.grid.rows));
      setVp({ ...v.vp, rowPx, colScale, leftX: 0, topPrice: v.grid.high, follow: false }, true);
    }),
    fitAxis: (axis) => withView((v) => {
      const fit = fitZoom(v.columns, v.grid, v.layout.width, v.layout.height, settingsRef.current.mode);
      setVp(axis === "y" ? { ...v.vp, rowPx: fit.rowPx, follow: false } : { ...v.vp, colScale: fit.colScale, follow: false });
      update(id, axis === "y" ? { profileZoomY: 0 } : { profileZoomX: 0 });
    }),
    edge: (which) => withView((v) => setVp({ ...v.vp, follow: false,
                                            leftX: which === "first" ? 0 : v.layout.contentW - v.layout.axisX })),
    hover: (x, y) => {
      const v = viewRef.current, tip = tipRef.current;
      if (!v || !tip) return;
      const i = v.m.xToSession(x);
      if (hoverRef.current !== i) {
        hoverRef.current = i;
        publishFocus(v.columns);
      }
      const at = v.m.yToPrice(y);
      const col = i === null ? null : v.columns[i];
      const row = col && at !== null ? rowAt(col, at, v.grid.rowSize) : null;
      if (!col || !row) {
        tip.hidden = true;
        return;
      }
      const d = settingsRef.current.units === "lots" ? settingsRef.current.lotSize : 1;
      tip.hidden = false;
      const s = settingsRef.current;
      const names = col.profile ? levelsOnRow(profileDrawing(col.profile, { enabled: s.levels, showPoc: s.showPoc,
        showValueArea: s.showValueArea, previous: v.columns.length === 1 }), row.price, v.grid.rowSize) : [];
      tip.textContent = `${columnName(col)} · ${fmtPrice(row.price)}  ${row.letters || "–"}  TPO ${row.tpo} · `
        + `vol ${compact(row.volume / d)} (buy ${compact(row.buy / d)} / sell ${compact(row.sell / d)}, Δ ${signed(row.delta / d)})`
        + (names.length ? ` · ${names.join(" · ")}` : "");
      tip.style.top = `${Math.min(y + 12, v.layout.height - 24)}px`;
    },
    leave: () => {
      if (tipRef.current) tipRef.current.hidden = true;
      hoverRef.current = null;
      if (viewRef.current) publishFocus(viewRef.current.columns);
    },
  };
  useProfileViewport(canvasRef, actions);

  // -- data and settings -------------------------------------------------------------------------

  useEffect(() => {
    const prevRow = settingsRef.current.rowSize;
    settingsRef.current = { showPoc: config.showPoc, showValueArea: config.showValueArea, mode: config.profileMode,
                            units, lotSize, rowSize: config.profileRow, zoomY: config.profileZoomY,
                            zoomX: config.profileZoomX, levels: config.profileLevels };
    const v = viewRef.current;
    if (v && prevRow !== config.profileRow && !v.vp.follow) {
      // new row size: keep the price at the centre of the screen and roughly the same price span
      const centre = v.vp.topPrice - visibleSpan(v.layout, prevRow) / 2;
      const rowPx = Math.max(2, Math.min(40, v.vp.rowPx * (config.profileRow / prevRow)));
      vpRef.current = { ...v.vp, rowPx, topPrice: centre + (v.layout.plotH / rowPx) * config.profileRow / 2 };
    }
    schedule();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [config.showPoc, config.showValueArea, config.profileMode, units, lotSize, config.profileRow,
      config.profileZoomY, config.profileZoomX, levelsKey]);

  useEffect(() => {
    const wrap = wrapRef.current;
    if (!wrap) return;
    const offLive = snapshotBus.subscribe(id, (snap) => {
      liveRef.current = { profile: snap.profile ?? null, replay: Boolean(snap.replay) };
      schedule();
    });
    const offHistory = historyBus.subscribe(id, (h) => {
      historyRef.current = h;
      schedule();
    });
    const resize = new ResizeObserver(() => schedule());
    resize.observe(wrap);
    return () => {
      offLive();
      offHistory();
      resize.disconnect();
      if (frameRef.current) cancelAnimationFrame(frameRef.current);
      frameRef.current = 0;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  useEffect(() => {                                       // a different set of days: start from today again
    vpRef.current = null;
    anchorRef.current = null;
    schedule();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [config.profileDays, config.mode, config.replayDate]);

  useEffect(() => {
    levelsRef.current = null;
    if (config.mode !== "live") return;
    let cancelled = false;
    const load = () => api.profileLevels(config.profileRow, config.profileComposite === "off" ? null : config.profileComposite)
      .then((r) => {
        if (cancelled) return;
        levelsRef.current = r;
        schedule();
      }).catch(() => undefined);
    void load();
    const timer = setInterval(load, 5 * 60_000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [config.mode, config.profileRow, config.profileComposite]);

  useEffect(() => () => {
    if (zoomSaveRef.current) clearTimeout(zoomSaveRef.current);
  }, []);

  // -- render ----------------------------------------------------------------------------------

  const d = units === "lots" ? lotSize : 1;
  const col = focus?.column;
  const p = col?.profile;
  const zoomBtn = "grid h-6 min-w-6 place-items-center rounded border border-line bg-panel-2/90 px-1.5 text-[11px] text-fg-2 hover:border-line-strong hover:text-fg";
  return (
    <div className="flex h-full flex-col" data-testid={`profile-${id}`}>
      <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-0.5 border-b border-line px-2 py-1 text-[11px] text-muted"
           aria-label={`Market profile figures ${id}`}>
        {col && <span className="font-semibold text-fg" aria-label="Focused session">{columnName(col)}</span>}
        {p?.stats ? (
          <>
            <Fig label="VPOC" value={fmtPrice(p.poc_volume)} />
            <Fig label="TPOC" value={fmtPrice(p.poc_tpo)} />
            <Fig label="VWAP" value={fmtPrice(p.vwap)} />
            <Fig label="TWAP" value={fmtPrice(p.twap)} />
            <Fig label="VA" value={p.value_area_volume ? `${fmtPrice(p.value_area_volume.low)}–${fmtPrice(p.value_area_volume.high)}` : "—"} />
            <Fig label="T-VA" value={p.value_area_tpo ? `${fmtPrice(p.value_area_tpo.low)}–${fmtPrice(p.value_area_tpo.high)}` : "—"} />
            <Fig label="IB" value={p.initial_balance?.high != null
              ? `${fmtPrice(p.initial_balance.low)}–${fmtPrice(p.initial_balance.high)}${p.initial_balance.final ? "" : " (forming)"}`
              : "—"} />
            {(p.ib_extensions?.reached_up || p.ib_extensions?.reached_down) && (
              <Fig label="IB ext" value={[p.ib_extensions.reached_up && `${p.ib_extensions.reached_up}× up`,
                                          p.ib_extensions.reached_down && `${p.ib_extensions.reached_down}× down`]
                .filter(Boolean).join(" · ")} />
            )}
            <Fig label="TPOs" value={String(p.stats.tpo_total)} />
            <Fig label="Vol" value={compact(p.stats.volume / d)} />
            <Fig label="Δ" value={signed(p.stats.delta / d)} tone={p.stats.delta >= 0 ? "buy" : "sell"} />
            {!!p.single_prints?.length && <Fig label="Singles" value={String(p.single_prints.length)} />}
            {(p.weak_high || p.weak_low) && (
              <Fig label="Weak" value={[p.weak_high && "high", p.weak_low && "low"].filter(Boolean).join(" · ")} />
            )}
            {focus?.single && p.previous && <Fig label="Prev POC" value={fmtPrice(p.previous.poc_tpo)} />}
          </>
        ) : (
          <span>{col?.state === "error" ? `Error: ${col.error}` : "Waiting for the first trade…"}</span>
        )}
        {!!focus?.excluded && (
          <span className="ml-auto" title={focus.excludedTitle}>{focus.excluded} session(s) not shown</span>
        )}
      </div>
      <div ref={wrapRef} className="relative min-h-0 flex-1">
        <canvas ref={canvasRef} tabIndex={0} className="absolute inset-0 cursor-grab touch-none outline-none active:cursor-grabbing"
                role="img" aria-label={`${multi ? "Multi-day market profile chart" : "Market profile chart"} ${id} (${config.profileMode})`} />
        <div ref={tipRef} hidden
             className="num pointer-events-none absolute left-2 z-10 rounded border border-line-strong bg-panel-2 px-2 py-0.5 text-[11px] text-fg" />
        <div className="absolute z-10 flex gap-1" style={{ right: 70, top: 40 }} role="group" aria-label={`Zoom ${id}`}>
          <button type="button" className={zoomBtn} aria-label="Zoom out" title="Zoom out (−, Ctrl+wheel)"
                  onClick={() => { const c = actions.centre(); actions.zoomBoth(1 / 1.25, c.x, c.y); }}>−</button>
          <button type="button" className={zoomBtn} aria-label="Zoom in" title="Zoom in (+, Ctrl+wheel)"
                  onClick={() => { const c = actions.centre(); actions.zoomBoth(1.25, c.x, c.y); }}>+</button>
          <button type="button" className={zoomBtn} aria-label="Fit all" title="Fit every day and the whole range (0)"
                  onClick={actions.fitAll}>Fit</button>
          <button type="button" className={`${zoomBtn} ${following ? "" : "border-accent/60 text-accent"}`}
                  aria-label="Follow today" aria-pressed={following} title="Follow today: last price centred (F, double-click)"
                  onClick={actions.follow}>Follow</button>
        </div>
      </div>
    </div>
  );
}

function columnName(col: DayColumn): string {
  if (col.state === "composite") {
    const c = (col.profile as ProfileLevelsResponse["composite"])?.composite;
    return c ? `Composite ${c.days} sessions (${shortDate(c.from)} – ${shortDate(c.to)})` : "Composite";
  }
  if (col.replay) return `Replay ${shortDate(col.date)}`;
  if (col.state === "live") return col.date === "today" ? "Today" : `Today ${shortDate(col.date)}`;
  return shortDate(col.date);
}

function Fig({ label, value, tone }: { label: string; value: string; tone?: "buy" | "sell" }) {
  return (
    <span>
      {label}{" "}
      <span className={`num ${tone === "buy" ? "text-buy" : tone === "sell" ? "text-sell" : "text-fg"}`}>{value}</span>
    </span>
  );
}

// -- drawing primitives -------------------------------------------------------------------------

/** One session's profile inside its own column. Nothing here reaches another column. */
function drawSession(ctx: CanvasRenderingContext2D, col: DayColumn, i: number, m: Mapper, l: MultiLayout,
                     opts: { showPoc: boolean; showValueArea: boolean; levels: readonly ProfileLevel[] }, single: boolean,
                     rowSize: number) {
  const x0 = m.sessionToX(i);
  const tpoX0 = x0 + l.pad, volX = tpoX0 + l.tpoW + l.gap;
  const bottom = l.plotTop + l.plotH;
  if (col.state === "live" && !col.replay && !single) {
    ctx.fillStyle = C.liveBg;
    ctx.fillRect(x0, l.plotTop, l.colW, l.plotH + l.footer);
  }
  if (!single) {
    ctx.strokeStyle = C.axis;
    ctx.beginPath();
    ctx.moveTo(Math.round(x0) + 0.5, l.plotTop);
    ctx.lineTo(Math.round(x0) + 0.5, bottom + l.footer);
    ctx.stroke();
  }
  const p = col.profile;
  if (!p || !p.rows.length) {
    ctx.fillStyle = C.muted;
    ctx.font = "11px ui-sans-serif, system-ui, sans-serif";
    ctx.fillText(col.state === "error" ? "Could not load" : "No trades yet", tpoX0, l.plotTop + 16);
    return;
  }
  const rowY = (price: number) => m.priceToY(price);
  const band = (lo: number, hi: number) => [rowY(hi), rowY(lo) + l.rowPx] as const;
  const right = volX + (l.showVolume ? l.volumeW : 0);

  const drawing = profileDrawing(p, { enabled: opts.levels, showPoc: opts.showPoc, showValueArea: opts.showValueArea,
                                       previous: single });
  for (const sh of drawing.shading) {                          // value areas, HVN bands: under the letters
    const [t, b] = band(sh.low, sh.high);
    ctx.fillStyle = sh.fill;
    ctx.fillRect(tpoX0 - 2, t, right - tpoX0 + 2, b - t);
    if (sh.edge) {                                             // HVN: a thick brown band
      ctx.fillStyle = sh.edge;
      ctx.fillRect(tpoX0 - 2, t, right - tpoX0 + 2, 2);
      ctx.fillRect(tpoX0 - 2, b - 2, right - tpoX0 + 2, 2);
    }
  }

  const maxVol = p.rows.reduce((mx, r) => Math.max(mx, r.volume), 0);      // this day's own scale
  const current = col.state === "live" ? (p.current_period ?? -1) : -1;
  ctx.textBaseline = "middle";
  ctx.font = `${l.fontPx}px ui-monospace, Consolas, monospace`;
  const h = Math.max(1, l.rowPx - (l.rowPx >= 4 ? 2 : 0));
  const maxTpo = col.state === "composite" ? p.rows.reduce((mx, r) => Math.max(mx, r.tpo), 0) : 0;
  for (const row of p.rows) {
    const y = rowY(row.price);
    if (y + l.rowPx < l.plotTop || y > bottom) continue;                  // only visible rows
    if (col.state === "composite") {                                      // merged days: TPO counts as bars
      if (maxTpo > 0 && row.tpo > 0) {
        ctx.fillStyle = LEVEL_COLORS.composite;
        ctx.globalAlpha = 0.65;
        ctx.fillRect(tpoX0, y + (l.rowPx >= 4 ? 1 : 0), Math.max(1, (row.tpo / maxTpo) * l.tpoW), h);
        ctx.globalAlpha = 1;
      }
    } else if (l.thinBars) {                                                     // extreme zoom-out: one bar per row
      ctx.fillStyle = C.text;
      ctx.globalAlpha = 0.6;
      ctx.fillRect(tpoX0, y, Math.max(1, row.letters.length * l.cellW), h);
      ctx.globalAlpha = 1;
    } else {
      for (let k = 0; k < row.letters.length; k++) {
        const ch = row.letters[k];
        const idx = LETTERS.indexOf(ch);
        const x = m.tpoX(i, k, ch);
        ctx.fillStyle = periodColor(idx);
        ctx.globalAlpha = idx === current || current < 0 ? 0.95 : 0.8;
        if (l.drawLetters) ctx.fillText(ch, x + 1, y + l.rowPx / 2);
        else ctx.fillRect(x, y + (l.rowPx >= 4 ? 1 : 0), Math.max(1, l.cellW - 1), h);
      }
      ctx.globalAlpha = 1;
    }
    if (l.showVolume && maxVol > 0 && row.volume > 0) {
      const total = (row.volume / maxVol) * l.volumeW, buyW = (row.buy / row.volume) * total;
      ctx.globalAlpha = 0.75;
      ctx.fillStyle = C.buy;
      ctx.fillRect(volX, y + (l.rowPx >= 4 ? 1 : 0), buyW, h);
      ctx.fillStyle = C.sell;
      ctx.fillRect(volX + buyW, y + (l.rowPx >= 4 ? 1 : 0), total - buyW, h);
      ctx.globalAlpha = 1;
    }
  }
  if (l.showVolume) {                                          // LVN ticks (HVNs are bands)
    ctx.fillStyle = C.lvn;
    for (const price of p.lvn ?? []) ctx.fillRect(volX - 4, rowY(price) + l.rowPx / 2 - 1, 3, 2);
  }
  drawLevels(ctx, drawing.lines, x0, tpoX0, right, l, m, rowSize, bottom);
  drawTrail(ctx, drawing.trail, p, tpoX0, l, m);
  if (drawing.weak.length && p.rows.length) {
    ctx.font = "9px ui-sans-serif, system-ui, sans-serif";
    ctx.fillStyle = LEVEL_COLORS.weak;
    for (const edge of drawing.weak) {
      const y = edge === "high" ? rowY(p.rows[0].price) : rowY(p.rows[p.rows.length - 1].price) + l.rowPx;
      ctx.setLineDash([2, 2]);
      ctx.strokeStyle = LEVEL_COLORS.weak;
      ctx.beginPath();
      ctx.moveTo(tpoX0, Math.round(y) + 0.5);
      ctx.lineTo(tpoX0 + l.tpoW, Math.round(y) + 0.5);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillText(edge === "high" ? "weak high" : "weak low", tpoX0 + 2, y + (edge === "high" ? -6 : 7));
    }
  }
  for (const sp of [...(p.single_prints ?? []), ...(p.tails ?? [])]) {
    const [t, b] = band(sp.low, sp.high);
    ctx.fillStyle = "kind" in sp ? C.muted : C.single;
    ctx.fillRect(x0 + l.pad * 0.5, t + 1, 2, b - t - 2);
  }
  const ib = p.initial_balance;
  if (ib?.high != null && ib.low != null) {
    const [t, b] = band(ib.low, ib.high);
    const bx = x0 + Math.max(2, l.pad * 0.28);
    ctx.strokeStyle = C.ib;
    ctx.setLineDash(ib.final ? [] : [3, 3]);
    ctx.beginPath();
    ctx.moveTo(bx + 2, t + 1);
    ctx.lineTo(bx, t + 1);
    ctx.lineTo(bx, b - 1);
    ctx.lineTo(bx + 2, b - 1);
    ctx.stroke();
    ctx.setLineDash([]);
  }
  if (l.mode === "split" && !l.thinBars) {
    for (const per of p.periods ?? []) {
      const x = tpoX0 + per.index * l.cellW;
      ctx.fillStyle = periodColor(per.index);
      ctx.fillRect(x - 1, rowY(per.open) + 1, 2, Math.max(2, l.rowPx - 2));
      ctx.fillRect(x + l.cellW - 2, rowY(per.close) + 1, 2, Math.max(2, l.rowPx - 2));
    }
  }
}

/** Naked POCs: dashed pink lines from their own session to the price axis; off-screen ones as edge markers. */
function drawNaked(ctx: CanvasRenderingContext2D, npocs: NakedPoc[], columns: DayColumn[], m: Mapper, l: MultiLayout) {
  const above: NakedPoc[] = [], below: NakedPoc[] = [];
  ctx.font = "9px ui-sans-serif, system-ui, sans-serif";
  ctx.textBaseline = "middle";
  for (const n of npocs) {
    const y = m.priceToY(n.price) + l.rowPx / 2;
    if (y < l.plotTop) { above.push(n); continue; }
    if (y > l.plotTop + l.plotH) { below.push(n); continue; }
    const i = columns.findIndex((c) => c.date === n.date);
    const x0 = Math.max(0, i >= 0 ? m.sessionToX(i) : 0);
    ctx.strokeStyle = LEVEL_COLORS.poc;
    ctx.lineWidth = 1.5;
    ctx.setLineDash([6, 4]);
    ctx.beginPath();
    ctx.moveTo(x0, Math.round(y) + 0.5);
    ctx.lineTo(l.axisX, Math.round(y) + 0.5);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.lineWidth = 1;
    const label = nakedLabel(n);
    ctx.fillStyle = LEVEL_COLORS.poc;
    ctx.fillText(label, l.axisX - ctx.measureText(label).width - 4, y - 6);
  }
  // off-screen: "▲ nPOC 13 Oct · 3d 22790" at the top / bottom edge, nearest first
  const edge = (list: NakedPoc[], top: boolean) => {
    list.sort((a, b) => (top ? a.price - b.price : b.price - a.price)).slice(0, 3).forEach((n, k) => {
      const text = `${top ? "▲" : "▼"} ${nakedLabel(n)} ${n.price}`;
      const y = top ? l.plotTop + 8 + k * 12 : l.plotTop + l.plotH - 8 - k * 12;
      ctx.fillStyle = "rgba(18,21,27,0.8)";
      const w = ctx.measureText(text).width;
      ctx.fillRect(l.axisX - w - 8, y - 6, w + 6, 12);
      ctx.fillStyle = LEVEL_COLORS.poc;
      ctx.fillText(text, l.axisX - w - 5, y);
    });
  };
  edge(above, true);
  edge(below, false);
}

/** y of a level: a row's centre, or (exact prices such as VWAP) where the price falls inside its row. */
function levelY(line: LevelLine, m: Mapper, l: MultiLayout, rowSize: number): number {
  const top = m.priceToY(line.price);
  if (!line.exact) return top + l.rowPx / 2;
  const floor = Math.floor(line.price / rowSize + 1e-9) * rowSize;
  return top + ((floor + rowSize - line.price) / rowSize) * l.rowPx;
}

/** Level lines over the letters, labelled at the column's right edge (labels on one height stack leftwards). */
function drawLevels(ctx: CanvasRenderingContext2D, lines: LevelLine[], x0: number, from: number, to: number,
                    l: MultiLayout, m: Mapper, rowSize: number, bottom: number) {
  const placed: { y: number; x: number }[] = [];
  const right = Math.min(x0 + l.colW, l.axisX) - 3;
  ctx.font = "9px ui-sans-serif, system-ui, sans-serif";
  ctx.textBaseline = "middle";
  for (const line of lines) {
    const y = levelY(line, m, l, rowSize);
    if (y < l.plotTop - 2 || y > bottom + 2) continue;
    ctx.strokeStyle = line.color;
    ctx.fillStyle = line.color;
    if (line.style === "band") {                               // VPOC / TPOC: a thick translucent pink row
      const h = Math.max(3, l.rowPx);
      ctx.globalAlpha = 0.38;
      ctx.fillRect(from - 2, y - h / 2, to - from + 2, h);
      ctx.globalAlpha = 1;
      ctx.fillRect(from - 4, y - h / 2, 2, h);
    } else {
      ctx.lineWidth = line.style === "thick" ? 3 : 1;
      ctx.setLineDash(line.style === "dashed" ? [4, 3] : line.style === "dotted" ? [1, 3] : []);
      ctx.beginPath();
      const yy = line.style === "thick" ? y : Math.round(y) + 0.5;
      ctx.moveTo(line.style === "dotted" ? x0 : from - 2, yy);
      ctx.lineTo(line.style === "dotted" ? right : to, yy);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.lineWidth = 1;
    }
    if (l.colW < 110) continue;                                // too narrow for labels
    const w = ctx.measureText(line.label).width;
    let x = right - w;
    for (const q of placed) if (Math.abs(q.y - y) < 9) x = Math.min(x, q.x - w - 4);
    if (x < from) continue;
    placed.push({ y, x });
    ctx.fillText(line.label, x, y - (line.style === "band" ? 0 : 6));
  }
}

/** The developing POC: where the volume POC stood at the end of each period, joined left to right. */
function drawTrail(ctx: CanvasRenderingContext2D, trail: { period: number; price: number }[], p: { periods?: unknown[] },
                   tpoX0: number, l: MultiLayout, m: Mapper) {
  if (trail.length < 1) return;
  const n = Math.max(trail.length, p.periods?.length ?? 0, 1);
  const xOf = (period: number) => l.mode === "split" ? tpoX0 + (period + 0.5) * l.cellW
    : tpoX0 + ((period + 0.5) / n) * l.tpoW;
  ctx.strokeStyle = LEVEL_COLORS.poc;
  ctx.fillStyle = LEVEL_COLORS.poc;
  ctx.setLineDash([2, 2]);
  ctx.beginPath();
  trail.forEach((t, k) => {
    const x = xOf(t.period), y = m.priceToY(t.price) + l.rowPx / 2;
    if (k) ctx.lineTo(x, y);
    else ctx.moveTo(x, y);
  });
  ctx.stroke();
  ctx.setLineDash([]);
  trail.forEach((t, k) => {
    const r = k === trail.length - 1 ? 3 : 1.6;
    ctx.beginPath();
    ctx.arc(xOf(t.period), m.priceToY(t.price) + l.rowPx / 2, r, 0, Math.PI * 2);
    ctx.fill();
  });
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
    const col = columns[i], colX = m.sessionToX(i);
    // sticky: a column wider than the view keeps its label in the visible part
    const x = Math.max(colX + 2, Math.min(Math.max(colX + l.pad, 4), colX + l.colW - 72));
    const live = col.state === "live";
    const full = l.colW >= 90, mid = l.colW >= 44;
    const date = shortDate(col.date);
    const comp = col.state === "composite" ? (col.profile as ProfileLevelsResponse["composite"])?.composite : null;
    const title = col.state === "composite" ? (full ? "COMPOSITE" : "COMP")
      : col.replay ? (full ? `REPLAY ${date}` : "REPLAY") : live ? "TODAY" : full || mid ? date : date.slice(0, 2);
    ctx.fillStyle = col.state === "composite" ? LEVEL_COLORS.composite
      : live && !col.replay ? C.live : col.replay ? C.warn : C.strong;
    ctx.font = "600 11px ui-sans-serif, system-ui, sans-serif";
    ctx.fillText(title, x, 12);
    if (full) {
      ctx.fillStyle = C.muted;
      ctx.font = "10px ui-sans-serif, system-ui, sans-serif";
      const sub = comp ? `${comp.days}d · ${shortDate(comp.from)}–${shortDate(comp.to)}`
        : col.state === "error" ? "error" : col.replay ? "replay" : live
        ? (col.date !== "today" ? `${date} · live` : "live") : col.date.slice(0, 4);
      ctx.fillText(sub, x, 25);
    }
    if (columns.length > 1) {
      ctx.strokeStyle = C.axis;
      ctx.beginPath();
      ctx.moveTo(Math.round(colX) + 0.5, 0);
      ctx.lineTo(Math.round(colX) + 0.5, l.plotTop);
      ctx.stroke();
    }
  }
  if (l.mode === "split" && l.footer) {                       // period letters under each split column
    ctx.font = "9px ui-monospace, Consolas, monospace";
    for (let i = first; i <= last; i++) {
      for (const per of columns[i].profile?.periods ?? []) {
        if (l.cellW < 8 && per.index % 2) continue;
        ctx.fillStyle = periodColor(per.index);
        ctx.fillText(per.letter, m.sessionToX(i) + l.pad + per.index * l.cellW + 2, l.plotTop + l.plotH + l.footer / 2);
      }
    }
  }
  ctx.restore();
  ctx.strokeStyle = C.axis;
  ctx.beginPath();
  ctx.moveTo(0, l.plotTop - 0.5);
  ctx.lineTo(l.axisX, l.plotTop - 0.5);
  ctx.stroke();
}

/** The one price axis (fixed while sessions pan). The live last price is boxed here only. */
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
  const step = labelStep(l.rowPx, grid.rowSize);
  const digits = grid.rowSize < 1 ? 2 : 0;
  const lastY = last == null ? null : m.priceToY(last) + l.rowPx / 2;
  ctx.fillStyle = C.text;
  const topVisible = m.yToPrice(l.plotTop) ?? grid.high;
  const from = Math.ceil((grid.low) / step) * step, to = Math.min(grid.high, topVisible + step);
  for (let price = from; price <= to + 1e-9; price += step) {
    const y = m.priceToY(price) + l.rowPx / 2;
    if (y < l.plotTop || y > l.plotTop + l.plotH) continue;
    if (lastY != null && Math.abs(y - lastY) < 14) continue;
    ctx.fillText(price.toFixed(digits), l.axisX + 6, y);
  }
  if (last != null && lastY != null && lastY >= l.plotTop && lastY <= l.plotTop + l.plotH) {
    ctx.fillStyle = C.strong;
    ctx.fillRect(l.axisX + 1, lastY - 8, l.width - l.axisX - 1, 16);
    ctx.fillStyle = C.bg;
    ctx.fillText(last.toFixed(2), l.axisX + 6, lastY);
  }
}

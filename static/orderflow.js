/* Order-flow chart page: draws the engine's footprint with OpenAlgo Charts'
   Footprint primitive, live (/ws/chart) or replayed (/api/replay/{date}/chart).
   Both deliver the same payload (server._chart_payload), so everything below
   the two data sources is shared. Engine -> library mapping: orderflow_adapter.js. */
import { createChart, darkTheme, lightTheme } from "/static/vendor/openalgo-charts/openalgo-charts.mjs";
import { Footprint, compactVol } from "/static/vendor/openalgo-charts/openalgo-charts.profile.mjs";
import { toFootprintBar, toCandle, hasOhlc, bandRange } from "/static/orderflow_adapter.js";

const OF = window.OFRender;
const $ = OF.$;
const MODE = new URLSearchParams(location.search).get("mode") === "replay" ? "replay" : "live";
const IST = "Asia/Kolkata";
const IST_OFFSET = "+05:30";
const DEFAULT_LOT = 65;
const DEFAULT_TICK = 0.1;

// ---------------------------------------------------------------- preferences
const pref = {
  get(key, fallback, allowed) {
    try {
      const v = localStorage.getItem("of." + key);
      if (v === null) return fallback;
      return allowed && !allowed.includes(v) ? fallback : v;
    } catch { return fallback; }
  },
  set(key, value) { try { localStorage.setItem("of." + key, String(value)); } catch { /* private mode */ } },
};
const bool = (key, fallback) => pref.get(key, fallback ? "1" : "0") === "1";

const palettes = {
  midnight: { bg: "#101216", panel: "#15181e", alt: "#1b1f27", border: "#272c35", text: "#d9dee7", muted: "#8993a2", buy: "#159d87", sell: "#cc374b", poc: "#ccd5e1", va: "#637184", cell: "#edf3fa", grid: "#15191f" },
  graphite: { bg: "#1a1b1e", panel: "#232428", alt: "#2b2c30", border: "#34363b", text: "#e1e2e5", muted: "#9b9da6", buy: "#4cad99", sell: "#d06575", poc: "#ddd8cc", va: "#797c87", cell: "#f5f5f6", grid: "#202226" },
  classic: { bg: "#050605", panel: "#111411", alt: "#181c18", border: "#293329", text: "#e5eae5", muted: "#929e92", buy: "#32e600", sell: "#ff2424", poc: "#fff4bf", va: "#818d73", cell: "#ffffff", grid: "#0c100c" },
  ocean: { bg: "#081825", panel: "#102232", alt: "#16304a", border: "#233b4e", text: "#daebf8", muted: "#86a3b8", buy: "#12b8c6", sell: "#ec6384", poc: "#f8d898", va: "#588299", cell: "#f1f8ff", grid: "#102131" },
  ivory: { light: true, bg: "#f6f4ee", panel: "#fffcf6", alt: "#efece3", border: "#dedacf", text: "#283139", muted: "#747569", buy: "#147e71", sell: "#bf4e59", poc: "#525d65", va: "#969b8e", cell: "#172b2b", grid: "#eeece5" },
};

// ------------------------------------------------------------- initial control state
$("style").value = pref.get("style", "profile", ["profile", "ladder", "heatmap"]);
$("mode").value = pref.get("values", "bidask", ["bidask", "delta", "volume"]);
$("text").value = pref.get("text", "contrast", ["contrast", "side", "delta", "dominant", "imbalance", "volume"]);
$("units").value = pref.get("units", "raw", ["raw", "lots"]);
$("ppr").value = pref.get("ppr", "1", ["1", "2", "3", "4", "5"]);
$("interval").value = pref.get("interval", "60", ["60", "180", "300", "900", "1800"]);
$("ratio").value = pref.get("ratio", "3", ["2", "3", "4", "5"]);
$("theme").value = pref.get("theme", "midnight", Object.keys(palettes));
for (const id of ["poc", "valuearea", "stacks", "stats"]) $(id).checked = bool(id, true);
$("table").checked = bool("table", false);
const tableInputs = [...document.querySelectorAll('input[name="table-row"]')];

// ------------------------------------------------------------------- runtime state
let lotSize = DEFAULT_LOT;
let tickSize = DEFAULT_TICK;
let rowSize = 1;
let lastSnap = null;
let bars = [];          // FootprintBar[]
let candles = [];
let prevTimes = [];
let forceFit = true;    // refit on the next snapshot (first data, ppr change, session change)

const pprValue = () => Number($("ppr").value);
const intervalValue = () => Number($("interval").value);   // seconds; must be one of config.ALLOWED_CHART_INTERVALS_SEC
const unitMode = () => ($("units").value === "lots" ? "lots" : "qty");
const divisor = () => ($("units").value === "lots" ? lotSize : 1);
const quantity = (v) => compactVol(v / divisor());
const signed = (v) => (v >= 0 ? "+" : "") + quantity(v);
const { fmtQty } = OF.makeQtyFormatters(unitMode, () => lotSize);
const selectedTableRows = () => tableInputs.filter((i) => i.checked).map((i) => i.value);
const activeTableRows = () => ($("table").checked ? selectedTableRows() : []);
const activeCards = () => ($("stats").checked ? ["volume", "delta", "deltaPct"] : []);

// ------------------------------------------------------------------------- chart
const chart = createChart($("chart"), {
  theme: darkTheme, priceAxisWidth: 74, priceFormatter: (v) => v.toFixed(2),
  timeScale: { maxBarSpacing: 260 },
  priceScale: { minMove: DEFAULT_TICK, marginTop: 0.1, marginBottom: 0.27 },
});
const clear = "rgba(0,0,0,0)";
// Actual OHLC feeds autoscale and the time axis; the footprint draws its own
// slim candle next to the ladder, so this series stays invisible.
const priceSeries = chart.addSeries("candlestick", { style: {
  upColor: clear, downColor: clear, wickUpColor: clear, wickDownColor: clear, borderUpColor: clear, borderDownColor: clear,
} });
const footprint = new Footprint({
  cellStyle: $("style").value, displayMode: $("mode").value, textColorMode: $("text").value,
  statsPosition: "bar", statsRows: activeCards(), tableRows: activeTableRows(), tableLabelWidth: 150,
  pocStyle: "outline", showPoc: $("poc").checked, showValueArea: $("valuearea").checked, valueAreaPercent: 0.7,
  imbalanceRatio: Number($("ratio").value), stackedImbalances: $("stacks").checked ? 3 : 0,
  widthFactor: 0.84, font: 11, radius: 1, statsRowHeight: 19, volumeDivisor: divisor(),
});
chart.addPrimitive(footprint);

// The library's footprint reports the price extent of EVERY loaded bar to the
// autoscaler. The server sends up to 60 columns but only a handful fit on
// screen, so the visible ladders would be squashed into a sliver of the axis
// (its own demo loads 7 bars and never shows this). Report only the columns in
// or next to view; the transparent price series already autoscales on what is
// visible.
const loadedExtent = footprint.autoscaleInfo.bind(footprint);
footprint.autoscaleInfo = () => {
  const range = safeRange();
  if (!range || !bars.length) return loadedExtent();
  const from = Math.max(0, Math.floor(range.from) - 1);
  const to = Math.min(bars.length - 1, Math.ceil(range.to) + 1);
  const half = rowSize / 2;
  let min = Infinity, max = -Infinity;
  for (let i = from; i <= to; i++) {
    for (const c of bars[i].cells) { min = Math.min(min, c.price - half); max = Math.max(max, c.price + half); }
    if (bars[i].low !== undefined) { min = Math.min(min, bars[i].low); max = Math.max(max, bars[i].high); }
  }
  return Number.isFinite(min) ? { min, max } : loadedExtent();
};

function fitColumns() {
  if (!bars.length) return;
  const minSpacing = 125; // room for complete labels and raw quantities
  const width = chart.timeScale.width;
  if (!activeTableRows().length) {
    const visible = Math.max(1, Math.min(bars.length, Math.floor(width / minSpacing - 0.45)));
    chart.timeScale.setVisibleLogicalRange({ from: bars.length - visible - 0.7, to: bars.length - 0.25 });
    return;
  }
  // Keep the fixed metric labels clear; narrow screens show fewer recent columns.
  const available = Math.max(70, width - 150);
  const visible = Math.max(1, Math.min(bars.length, Math.floor(available / minSpacing - 0.5)));
  const spacing = Math.max(minSpacing, available / (visible + 0.5));
  const to = bars.length - 0.25;
  chart.timeScale.setVisibleLogicalRange({ from: to - width / spacing, to });
}

function applyTableLayout(fit = true) {
  const tableRows = activeTableRows();
  const cardsHeight = $("stats").checked ? 3 * 19 : 0;
  const tableHeight = tableRows.length * 19;
  $("chart-wrap").style.minHeight = (tableRows.length ? tableHeight + cardsHeight + 320 : 320) + "px";
  const plotHeight = Math.max(1, $("chart").clientHeight - 22);
  const marginBottom = tableRows.length ? Math.min(0.65, (tableHeight + cardsHeight + 35) / plotHeight) : 0.27;
  chart.setPriceScaleOptions({ marginBottom });
  footprint.setOptions({ tableRows, statsRows: activeCards() });
  // Keep the corner mark clear of the table's metric labels.
  const position = tableRows.length ? "top-left" : "bottom-left";
  if (chart.brandingOptions().position !== position) chart.setBranding({ position });
  $("table-count").textContent = selectedTableRows().length;
  if (fit) fitColumns();
}

function applyTheme() {
  const t = palettes[$("theme").value];
  const root = document.documentElement;
  root.style.colorScheme = t.light ? "light" : "dark";
  for (const [k, v] of Object.entries({
    bg: t.bg, panel: t.panel, "panel-alt": t.alt, border: t.border, text: t.text, "text-dim": t.muted,
    "green-bright": t.buy, "red-bright": t.sell, green: t.buy, red: t.sell,
  })) root.style.setProperty("--" + k, v);
  chart.setTheme({
    ...(t.light ? lightTheme : darkTheme), background: t.bg, grid: t.grid, axisLine: t.border, axisText: t.muted,
    buy: t.buy, sell: t.sell, upColor: t.buy, downColor: t.sell, wickUpColor: t.buy, wickDownColor: t.sell,
    lastPriceUp: t.buy, lastPriceDown: t.sell, lastPriceText: t.bg,
  });
  footprint.setOptions({
    buyColor: t.buy, sellColor: t.sell, pocColor: t.poc, valueAreaColor: t.va, textColor: t.cell,
    buyTextColor: t.light ? "#075344" : "#8aefcd", sellTextColor: t.light ? "#8d142c" : "#ffb2bd",
  });
  $("tip").hidden = true;
}

// ---------------------------------------------------------------- snapshot -> chart
function safeRange() {
  try { return chart.getVisibleLogicalRange(); } catch { return null; }
}

function applySnapshot(snap) {
  lastSnap = snap;
  const ch = snap.chart;
  rowSize = ch.row_size;
  tickSize = snap.status?.tick_size ?? snap.session?.tick_size ?? tickSize;
  const lot = snap.status?.lot_size ?? snap.lot_size;
  if (lot && lot !== lotSize) { lotSize = lot; applyUnits(); }

  // Where is the user looking? Follow live data only if they are at the right edge.
  const range = safeRange();
  const following = !prevTimes.length || !range || range.to >= prevTimes.length - 1.5;

  const usable = ch.bars.filter(hasOhlc);
  bars = usable.map((b) => toFootprintBar(b, rowSize));
  candles = usable.map(toCandle);
  const times = bars.map((b) => b.time);

  priceSeries.setData(candles);
  footprint.setOptions({ cvdOffset: ch.cvd_offset, tickSize: rowSize });
  footprint.setBars(bars);

  if (times.length) {
    const newBar = times[times.length - 1] !== prevTimes[prevTimes.length - 1];
    if (forceFit || (newBar && following)) {
      fitColumns();
      forceFit = false;
    } else if (!following && range && prevTimes.length) {
      // The chart keeps its distance from the RIGHT edge when data changes, so
      // new columns would scroll what the user is inspecting away. Put the same
      // columns (by time) back under the same part of the screen. `offset` is
      // where the old first column sits now: positive when older columns were
      // prepended, negative when the server's window slid past some.
      const at = times.indexOf(prevTimes[0]);
      const offset = at >= 0 ? at : -prevTimes.filter((t) => t < times[0]).length;
      const now = safeRange();
      if (now && (Math.abs(now.from - (range.from + offset)) > 1e-6 || Math.abs(now.to - (range.to + offset)) > 1e-6)) {
        chart.setVisibleLogicalRange({ from: range.from + offset, to: range.to + offset });
      }
    }
  }
  prevTimes = times;

  $("empty").hidden = bars.length > 0;
  $("empty").textContent = MODE === "replay" ? "No trades at or before the cursor yet." : "Waiting for the first trade…";
  renderChrome(snap);
}

function renderChrome(snap) {
  const badge = $("source-badge");
  badge.textContent = "sides: " + snap.source.side_basis;
  badge.title = "BUY/SELL are inferred per trade by the midpoint rule (price below mid = BUY), a reverse-engineered hypothesis.";

  OF.renderOrderBook(snap.book, fmtQty);
  $("book-note").textContent = MODE === "replay" ? "(as of last trade)" : "";

  const stats = footprint.stats().at(-1);
  const last = candles.at(-1);
  if (snap.status) {
    $("symbol").textContent = snap.status.symbol || "—";
    if (MODE === "live") setFeedStatus(snap.status);
  }
  const ltp = snap.quote?.ltp ?? last?.close;
  $("s-last").textContent = Number.isFinite(ltp) ? ltp.toFixed(2) : "—";
  if (stats) {
    $("s-vol").textContent = quantity(stats.volume);
    for (const [id, v] of [["s-delta", stats.delta], ["s-cvd", stats.cvd]]) {
      $(id).textContent = signed(v);
      $(id).className = v >= 0 ? "up" : "down";
    }
  } else {
    for (const id of ["s-vol", "s-delta", "s-cvd"]) { $(id).textContent = "—"; $(id).className = ""; }
  }
}

function setFeedStatus(status) {
  const dot = $("status-dot");
  if (status.connected) {
    dot.className = "dot connected";
    $("status-text").textContent = "feed connected";
  } else {
    dot.className = "dot disconnected";
    $("status-text").textContent = status.error ? `error: ${status.error}` : "feed disconnected";
  }
}

function applyUnits() {
  footprint.setOptions({ volumeDivisor: divisor() });
  const unit = $("units").value === "lots" ? "lots" : "qty";
  $("volume-label").textContent = "Bar volume · " + unit;
  $("delta-label").textContent = "Bar delta · " + unit;
  $("cvd-label").textContent = (MODE === "live" ? "CVD since server start · " : "CVD since first saved trade · ") + unit;
  if (lastSnap) renderChrome(lastSnap);
  $("tip").hidden = true;
}

// ------------------------------------------------------------------------ tooltip
chart.on("crosshair:move", (event) => {
  const tip = $("tip");
  const hit = event.point ? footprint.hoverAt(event.point.x, event.point.y) : null;
  if (!hit) { tip.hidden = true; return; }
  const q = (v) => (v / divisor()).toLocaleString("en-US", { maximumFractionDigits: 4 });
  const sq = (v) => (v >= 0 ? "+" : "") + q(v);
  const row = (label, value) => `<div class="row"><span>${label}</span><b>${value}</b></div>`;
  const when = new Date(hit.time * 1000).toLocaleTimeString("en-GB", { timeZone: IST, hour12: false, hour: "2-digit", minute: "2-digit" });
  let html = `<div class="title">${when} IST · ${$("units").value === "lots" ? `lots (${lotSize} qty/lot)` : "raw quantity"}</div>`;
  if (hit.cell) {
    const [lo, hi] = bandRange(hit.cell.price, rowSize, tickSize);
    html += row("Price", lo.toFixed(2) + (hi > lo ? " – " + hi.toFixed(2) : ""));
    html += row("Sell × Buy", q(hit.cell.bidVol) + " × " + q(hit.cell.askVol));
  }
  html += row("Volume", q(hit.stats.volume)) + row("Buy volume", q(hit.stats.askVolume)) + row("Sell volume", q(hit.stats.bidVolume))
    + row("Delta", sq(hit.stats.delta))
    + row("Min delta", hit.stats.minDelta === null ? "—" : sq(hit.stats.minDelta))
    + row("Max delta", hit.stats.maxDelta === null ? "—" : sq(hit.stats.maxDelta))
    + row("Delta %", hit.stats.deltaPct.toFixed(1) + "%") + row("CVD", sq(hit.stats.cvd))
    + row("Trades", hit.stats.trades ?? "—");
  tip.innerHTML = html;
  tip.hidden = false;
  const rect = $("chart").getBoundingClientRect();
  tip.style.left = Math.max(4, Math.min(event.point.x + 16, rect.width - tip.offsetWidth - 8)) + "px";
  tip.style.top = Math.max(4, Math.min(event.point.y + 16, rect.height - tip.offsetHeight - 8)) + "px";
});

// -------------------------------------------------------------------- live source
let ws = null;

function sendState() {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ ppr: pprValue(), interval: intervalValue() }));
}

function connectLive() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const sock = new WebSocket(`${proto}://${location.host}/ws/chart`);
  ws = sock;
  sock.onopen = () => sendState();
  sock.onclose = () => {
    $("status-dot").className = "dot disconnected";
    $("status-text").textContent = "reconnecting…";
    setTimeout(connectLive, 2000);
  };
  sock.onerror = () => sock.close();
  sock.onmessage = (evt) => {
    const snap = JSON.parse(evt.data);
    if (snap.ready) applySnapshot(snap);
  };
}

// ------------------------------------------------------------------ replay source
const replay = { date: null, meta: null, cursorMs: null, timer: null, seq: 0, busy: false };

const fmtClock = (ms) => {
  if (ms == null) return "—";
  const d = new Date(ms);
  return d.toLocaleTimeString("en-GB", { timeZone: IST, hour12: false }) + "." + String(d.getMilliseconds()).padStart(3, "0") + " IST";
};

function setReplayStatus(text, ok) {
  $("status-text").textContent = text;
  $("status-dot").className = "dot " + (ok ? "connected" : "disconnected");
}

async function loadSessions() {
  const data = await (await fetch("/api/replay/sessions")).json();
  const select = $("session-select");
  select.innerHTML = "";
  if (!data.sessions?.length) {
    select.innerHTML = "<option value=''>— no sessions logged yet —</option>";
    setReplayStatus("no saved sessions", false);
    return;
  }
  for (const date of data.sessions) select.appendChild(new Option(date, date));
  select.value = data.sessions[data.sessions.length - 1];
  await selectSession(select.value);
}

async function selectSession(date) {
  stopPlayback();
  replay.date = date;
  if (!date) return;
  const meta = await (await fetch(`/api/replay/${date}/meta`)).json();
  if (!meta.found) { setReplayStatus(`no trades logged for ${date}`, false); return; }
  replay.meta = meta;
  $("session-meta").textContent = `${meta.trade_count} records · ${fmtClock(meta.start_ms)} → ${fmtClock(meta.end_ms)}`;
  const slider = $("rp-slider");
  slider.min = meta.start_ms; slider.max = meta.end_ms; slider.disabled = false;
  for (const id of ["rp-prev", "rp-next", "rp-play"]) $(id).disabled = false;
  replay.cursorMs = meta.start_ms;
  slider.value = replay.cursorMs;
  prevTimes = [];
  forceFit = true;
  setReplayStatus(`replaying ${date}`, true);
  await fetchReplay();
}

async function fetchReplay() {
  if (!replay.date || replay.cursorMs === null) return;
  const seq = ++replay.seq;
  replay.busy = true;
  try {
    const url = `/api/replay/${replay.date}/chart?as_of_ms=${replay.cursorMs}&ppr=${pprValue()}&interval=${intervalValue()}`;
    const snap = await (await fetch(url)).json();
    if (seq !== replay.seq || !snap.ready) return;   // a newer request already landed
    applySnapshot(snap);
    renderReplayChrome(snap);
  } catch (err) {
    setReplayStatus("replay request failed: " + err.message, false);
  } finally {
    if (seq === replay.seq) replay.busy = false;
  }
}

function renderReplayChrome(snap) {
  $("symbol").textContent = "Replay " + snap.session.date;
  $("rp-slider").value = snap.session.cursor_ms;
  $("rp-cursor-time").textContent = fmtClock(snap.session.cursor_ms);
  const t = snap.cursor_trade;
  $("rp-trade-card").textContent = t
    ? `#${t.trade_id} · ${fmtClock(t.ts_ms)} · ${t.algo_side} ${fmtQty(t.qty)} @ ${t.ltp.toFixed(2)}`
      + (t.algo_reason ? ` (${t.algo_reason})` : "")
    : "No trade at cursor yet.";
}

function stepToTrade(which) {
  const target = lastSnap?.[which];
  if (!target) return;
  stopPlayback();
  replay.cursorMs = target.ts_ms;
  fetchReplay();
}

function stopPlayback() {
  if (replay.timer) { clearInterval(replay.timer); replay.timer = null; }
  $("rp-play").textContent = "▶ Play";
}

function togglePlayback() {
  if (replay.timer) { stopPlayback(); return; }
  if (!replay.meta) return;
  const speed = parseFloat($("rp-speed").value) || 1;
  const stepMs = 250;
  $("rp-play").textContent = "⏸ Pause";
  replay.timer = setInterval(() => {
    if (replay.busy) return;   // don't pile requests up behind a slow one
    replay.cursorMs = Math.min(replay.cursorMs + stepMs * speed, replay.meta.end_ms);
    if (replay.cursorMs >= replay.meta.end_ms) stopPlayback();
    fetchReplay();
  }, stepMs);
}

function gotoTypedTime() {
  if (!replay.date || !replay.meta) return;
  const m = $("rp-goto").value.trim().match(/^(\d{1,2}):(\d{2})(?::(\d{2})(?:\.(\d{1,3}))?)?$/);
  if (!m) { $("rp-goto").classList.add("invalid"); return; }
  $("rp-goto").classList.remove("invalid");
  const [, hh, mm, ss = "0", ms = "0"] = m;
  // Exchange time, not the browser's: the session is an IST trading day.
  const iso = `${replay.date}T${hh.padStart(2, "0")}:${mm}:${ss.padStart(2, "0")}.${ms.padEnd(3, "0")}${IST_OFFSET}`;
  stopPlayback();
  replay.cursorMs = Math.min(Math.max(Date.parse(iso), replay.meta.start_ms), replay.meta.end_ms);
  fetchReplay();
}

// --------------------------------------------------------------------- wiring
function refresh() {
  forceFit = true;
  if (MODE === "live") sendState(); else fetchReplay();
}

const persist = (key, el) => el.addEventListener("change", () => pref.set(key, el.type === "checkbox" ? (el.checked ? "1" : "0") : el.value));
for (const [key, id] of [["style", "style"], ["values", "mode"], ["text", "text"], ["units", "units"], ["ppr", "ppr"],
  ["interval", "interval"], ["ratio", "ratio"], ["theme", "theme"], ["poc", "poc"], ["valuearea", "valuearea"],
  ["stacks", "stacks"], ["stats", "stats"], ["table", "table"]]) persist(key, $(id));

$("style").addEventListener("change", () => footprint.setOptions({ cellStyle: $("style").value }));
$("mode").addEventListener("change", () => footprint.setOptions({ displayMode: $("mode").value }));
$("text").addEventListener("change", () => footprint.setOptions({ textColorMode: $("text").value }));
$("units").addEventListener("change", applyUnits);
$("ppr").addEventListener("change", refresh);
$("interval").addEventListener("change", refresh);
$("ratio").addEventListener("change", () => footprint.setOptions({ imbalanceRatio: Number($("ratio").value) }));
$("poc").addEventListener("change", () => footprint.setOptions({ showPoc: $("poc").checked }));
$("valuearea").addEventListener("change", () => footprint.setOptions({ showValueArea: $("valuearea").checked }));
$("stacks").addEventListener("change", () => footprint.setOptions({ stackedImbalances: $("stacks").checked ? 3 : 0 }));
$("stats").addEventListener("change", () => applyTableLayout(false));
$("table").addEventListener("change", () => applyTableLayout());
for (const input of tableInputs) input.addEventListener("change", () => applyTableLayout(false));
$("theme").addEventListener("change", applyTheme);
$("fit").addEventListener("click", fitColumns);

$("table-options").addEventListener("toggle", () => {
  const bounds = $("table-options").getBoundingClientRect();
  const chooser = $("table-options").querySelector("fieldset");
  chooser.style.left = Math.max(12, Math.min(bounds.left, innerWidth - 242)) + "px";
  chooser.style.top = (bounds.bottom + 6) + "px";
});
$("table-close").addEventListener("click", () => { $("table-options").open = false; });
document.addEventListener("pointerdown", (e) => { if (!$("table-options").contains(e.target)) $("table-options").open = false; });

let previousWidth = $("chart").clientWidth;
const resizeObserver = new ResizeObserver(() => {
  const width = $("chart").clientWidth;
  applyTableLayout(width !== previousWidth);
  previousWidth = width;
});
resizeObserver.observe($("chart"));
window.addEventListener("pagehide", () => { stopPlayback(); resizeObserver.disconnect(); });

if (MODE === "replay") {
  $("replay-bar").hidden = false;
  $("contract-switch").hidden = true;   // replay browses saved SESSIONS, not a live contract
  $("nav-replay").classList.add("active");
  document.title = "NIFTY FUT — Order Flow Chart (replay)";
  $("session-select").addEventListener("change", (e) => selectSession(e.target.value));
  $("rp-slider").addEventListener("input", (() => {
    let debounce = null;
    return (e) => {
      stopPlayback();
      replay.cursorMs = Number(e.target.value);
      $("rp-cursor-time").textContent = fmtClock(replay.cursorMs);
      clearTimeout(debounce);
      debounce = setTimeout(fetchReplay, 80);
    };
  })());
  $("rp-play").addEventListener("click", togglePlayback);
  $("rp-prev").addEventListener("click", () => stepToTrade("prev_trade"));
  $("rp-next").addEventListener("click", () => stepToTrade("next_trade"));
  $("rp-goto-btn").addEventListener("click", gotoTypedTime);
  $("rp-goto").addEventListener("keydown", (e) => { if (e.key === "Enter") gotoTypedTime(); });
  document.addEventListener("keydown", (e) => {
    if (e.target instanceof HTMLInputElement || e.target instanceof HTMLSelectElement) return;
    if (e.key === "ArrowLeft") stepToTrade("prev_trade");
    else if (e.key === "ArrowRight") stepToTrade("next_trade");
  });
} else {
  $("nav-live").classList.add("active");
}

applyTheme();
applyUnits();
applyTableLayout(false);
if (MODE === "live") {
  OF.initContractSwitcher("contract-select");
  connectLive();
} else {
  loadSessions();
}

// Read-only inspection hooks for browser checks (same idea as the library's own example).
window.__of = { chart, footprint, priceSeries, snapshot: () => lastSnap, data: () => ({ bars, candles }), replay };

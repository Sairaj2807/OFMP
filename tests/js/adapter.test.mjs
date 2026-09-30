// Engine payload -> adapter -> the REAL built Footprint primitive (no DOM needed
// for its stats). Run from the repo root:  node --test tests/js/
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const lib = (f) => pathToFileURL(resolve(root, "static/vendor/openalgo-charts", f)).href;
const { Footprint, stackedImbalances } = await import(lib("openalgo-charts.profile.mjs"));
const { toFootprintBar, toCandle, bandRange } = await import(pathToFileURL(resolve(root, "static/orderflow_adapter.js")).href);

const skip = false;
const PPR = 2;
const fixture = skip ? null : JSON.parse(execFileSync("python", ["-B", "tests/dump_chart_fixture.py", String(PPR)],
  { cwd: root, maxBuffer: 256 * 1024 * 1024, stdio: ["ignore", "pipe", "ignore"] }).toString());

const round8 = (v) => Math.round(v * 1e8) / 1e8;
const sum = (xs) => xs.reduce((a, b) => a + b, 0);

function primitiveFor(payload) {
  const fp = new Footprint({ tickSize: payload.row_size, cvdOffset: payload.cvd_offset, valueAreaPercent: 0.7 });
  const bars = payload.bars.map((b) => toFootprintBar(b, payload.row_size));
  fp.setBars(bars);
  return { fp, bars };
}

test("adapter: BUY goes to askVol, SELL to bidVol, prices are band centres", { skip }, () => {
  const { full } = fixture;
  const bar = full.bars[0];
  const out = toFootprintBar(bar, full.row_size);
  assert.equal(out.cells.length, bar.cells.length);
  bar.cells.forEach((c, i) => {
    assert.equal(out.cells[i].askVol, c.buy);
    assert.equal(out.cells[i].bidVol, c.sell);
    assert.equal(round8(out.cells[i].price - full.row_size / 2), c.price);
    const [lo, hi] = bandRange(out.cells[i].price, full.row_size, fixture.tick);
    assert.equal(lo, c.price);
    assert.equal(hi, round8(c.price + full.row_size - fixture.tick));
  });
  assert.equal(out.delta, bar.delta);
  assert.equal(out.rowSize, full.row_size);
  assert.equal(toCandle(bar).volume, sum(bar.cells.map((c) => c.buy + c.sell)));
});

test("adapter: missing metadata stays missing, never zero", () => {
  const out = toFootprintBar({ time: 60, cells: [], delta: 0, open: null, high: null, low: null, close: null,
    trades: null, min_delta: null, max_delta: null }, 1);
  for (const k of ["open", "high", "low", "close", "tradeCount", "minDelta", "maxDelta"]) assert.ok(!(k in out), k);
});

test("library stats agree with the engine: volume, sides, delta, min/max delta, trades", { skip }, () => {
  const { fp } = primitiveFor(fixture.full);
  const stats = fp.stats();
  assert.equal(stats.length, fixture.full.bars.length);
  stats.forEach((s, i) => {
    const b = fixture.full.bars[i];
    assert.equal(s.time, b.time);
    assert.equal(s.askVolume, sum(b.cells.map((c) => c.buy)), "buy volume");
    assert.equal(s.bidVolume, sum(b.cells.map((c) => c.sell)), "sell volume");
    assert.equal(s.volume, s.askVolume + s.bidVolume);
    assert.equal(s.delta, b.delta);
    assert.equal(s.minDelta, b.min_delta);
    assert.equal(s.maxDelta, b.max_delta);
    assert.equal(s.trades, b.trades);
  });
});

test("library CVD equals the true running total, for a full window and a sliding one", { skip }, () => {
  const total = sum(fixture.full.bars.map((b) => b.delta));
  assert.equal(primitiveFor(fixture.full).fp.stats().at(-1).cvd, total);
  // 10-bar window: cvd_offset must carry everything that scrolled out.
  assert.equal(fixture.last10.bars.length, 10);
  assert.equal(primitiveFor(fixture.last10).fp.stats().at(-1).cvd, total);
  // and per bar, each window column agrees with the full series
  const full = primitiveFor(fixture.full).fp.stats();
  primitiveFor(fixture.last10).fp.stats().forEach((s, i) => assert.equal(s.cvd, full[full.length - 10 + i].cvd));
});

test("library POC equals the engine POC on every candle", { skip }, () => {
  const { fp } = primitiveFor(fixture.full);
  for (const s of fp.stats()) {
    assert.equal(round8(s.poc - fixture.full.row_size / 2), fixture.engine[String(s.time)].poc, `poc @ ${s.time}`);
  }
});

// The engine and the library define value area and stacked imbalances slightly
// differently (documented in the plan: value-area ties, sell-imbalance diagonal,
// zero-volume handling, stack adjacency). These are NOT asserted equal; they are
// measured and printed, so the size of the difference is on record.
test("report: value area and stacked imbalances, engine vs library", { skip }, () => {
  const { fp, bars } = primitiveFor(fixture.full);
  const half = fixture.full.row_size / 2;
  let vaSame = 0;
  fp.stats().forEach((s) => {
    const [lo, hi] = fixture.engine[String(s.time)].value_area;
    const poc = fixture.engine[String(s.time)].poc;
    assert.ok(lo <= poc && poc <= hi, "engine VA contains its POC");
    assert.ok(s.val <= s.poc && s.poc <= s.vah, "library VA contains its POC");
    if (round8(s.val - half) === lo && round8(s.vah - half) === hi) vaSame++;
  });

  let bothNone = 0, identical = 0, engineAny = 0, libAny = 0;
  for (const bar of bars) {
    const libStacks = stackedImbalances(bar.cells, 3, 3, fixture.full.row_size)
      .map((s) => `${s.side}:${round8(s.startPrice - half)}-${round8(s.endPrice - half)}`).sort();
    const eng = fixture.engine[String(bar.time)].stacks
      .map((st) => `${st[0][0] === "BUY_IMBALANCE" ? "buy" : "sell"}:${Math.max(...st.map((x) => x[1]))}-${Math.min(...st.map((x) => x[1]))}`).sort();
    if (eng.length) engineAny++;
    if (libStacks.length) libAny++;
    if (!eng.length && !libStacks.length) bothNone++;
    else if (JSON.stringify(eng) === JSON.stringify(libStacks)) identical++;
  }
  console.log(`\n  parity @ ${PPR} price/row over ${bars.length} candles:`
    + `\n    value area identical:      ${vaSame}/${bars.length}`
    + `\n    stacks: engine has some in ${engineAny}, library in ${libAny}; both none ${bothNone}; identical non-empty ${identical}`);
});

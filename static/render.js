/* Shared rendering helpers used by both the live dashboard (app.js) and the
   Orderflow Replay verification view (replay.js). Pure functions: callers
   own their own state (unit mode, lot size, last snapshot) and pass in
   already-computed data plus a couple of DOM-id overrides — nothing here
   holds state, so it's safe to load into both pages independently. */
(() => {
  const $ = (id) => document.getElementById(id);
  const fmt = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v)) ? "—" : Number(v).toFixed(d);
  const fmtInt = (v) => (v === null || v === undefined) ? "—" : Number(v).toLocaleString();

  // Returns {fmtQty, fmtSignedQty} bound to the caller's own unit-mode /
  // lot-size getters, so each page can keep its own toggle state.
  function makeQtyFormatters(getUnitMode, getLotSize) {
    function toUnits(v) {
      if (v === null || v === undefined || Number.isNaN(v)) return v;
      if (getUnitMode() === "lots" && getLotSize() > 0) return v / getLotSize();
      return v;
    }
    function fmtQty(v) {
      const val = toUnits(v);
      if (val === null || val === undefined || Number.isNaN(val)) return "—";
      return Number.isInteger(val) ? val.toLocaleString() : val.toFixed(2);
    }
    function fmtSignedQty(v) {
      const val = toUnits(v);
      if (val === null || val === undefined || Number.isNaN(val)) return "—";
      const sign = val >= 0 ? "+" : "";
      return sign + (Number.isInteger(val) ? val.toLocaleString() : val.toFixed(2));
    }
    return { fmtQty, fmtSignedQty };
  }

  function renderOrderBook(book, fmtQty, ids = {}) {
    const {
      spreadId = "ob-spread", midId = "ob-mid", microId = "ob-micro", obiId = "ob-obi",
      obiFillId = "obi-fill", asksBodyId = "ob-asks-body", bidsBodyId = "ob-bids-body",
    } = ids;
    if (!book) return;
    $(spreadId).textContent = fmt(book.spread);
    $(midId).textContent = fmt(book.mid);
    $(microId).textContent = fmt(book.micro);
    $(obiId).textContent = fmt(book.obi);

    const obiFill = $(obiFillId);
    const pct = Math.min(Math.abs(book.obi || 0), 1) * 50;
    obiFill.style.width = pct + "%";
    if (book.obi >= 0) {
      obiFill.classList.remove("negative");
      obiFill.style.left = "50%";
    } else {
      obiFill.classList.add("negative");
      obiFill.style.left = (50 - pct) + "%";
    }

    const maxQty = Math.max(1, ...book.bids.map((b) => b[1]), ...book.asks.map((a) => a[1]));

    const asksBody = $(asksBodyId);
    const bidsBody = $(bidsBodyId);
    asksBody.innerHTML = "";
    bidsBody.innerHTML = "";

    const asksSorted = [...book.asks].sort((a, b) => b[0] - a[0]);
    for (const [price, qty, orders] of asksSorted) {
      const tr = document.createElement("tr");
      tr.className = "ask";
      const barWidth = (qty / maxQty) * 100;
      tr.innerHTML = `
        <td></td><td></td>
        <td class="price">${fmt(price)}<div class="depth-bar" style="width:${barWidth}%"></div></td>
        <td class="qty">${fmtQty(qty)}</td><td class="orders">${fmtInt(orders)}</td><td></td>`;
      asksBody.appendChild(tr);
    }

    const spacer = document.createElement("tr");
    spacer.className = "spacer-row";
    spacer.innerHTML = `<td colspan="6">spread ${fmt(book.spread)} · mid ${fmt(book.mid)}</td>`;
    asksBody.appendChild(spacer);

    const bidsSorted = [...book.bids].sort((a, b) => b[0] - a[0]);
    for (const [price, qty, orders] of bidsSorted) {
      const tr = document.createElement("tr");
      tr.className = "bid";
      const barWidth = (qty / maxQty) * 100;
      tr.innerHTML = `
        <td class="orders">${fmtInt(orders)}</td><td class="qty">${fmtQty(qty)}</td>
        <td class="price">${fmt(price)}<div class="depth-bar" style="width:${barWidth}%"></div></td>
        <td></td><td></td><td></td>`;
      bidsBody.appendChild(tr);
    }
  }

  function drawCvdSparkline(candles, canvasId = "cvd-canvas") {
    const canvas = $(canvasId);
    const ctx = canvas.getContext("2d");
    const w = canvas.width, h = canvas.height;
    ctx.clearRect(0, 0, w, h);

    const series = candles.map((c) => c.cvd).filter((v) => v !== null && v !== undefined);
    if (series.length < 2) return;

    const min = Math.min(...series, 0);
    const max = Math.max(...series, 0);
    const range = (max - min) || 1;
    const stepX = w / (series.length - 1);

    ctx.strokeStyle = "#388bfd";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    series.forEach((v, i) => {
      const x = i * stepX;
      const y = h - ((v - min) / range) * (h - 8) - 4;
      i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
    });
    ctx.stroke();

    const zeroY = h - ((0 - min) / range) * (h - 8) - 4;
    ctx.strokeStyle = "rgba(255,255,255,0.15)";
    ctx.beginPath();
    ctx.moveTo(0, zeroY);
    ctx.lineTo(w, zeroY);
    ctx.stroke();
  }

  function buildFootprintTable(candles, fmtQty, fmtSignedQty, tableId = "fp-table") {
    const table = $(tableId);
    const shown = candles.slice(-15);
    table.innerHTML = "";
    if (!shown.length) return;

    const priceSet = new Set();
    for (const c of shown) {
      for (const row of c.rows) priceSet.add(row.price);
    }
    const prices = [...priceSet].sort((a, b) => b - a);

    const thead = document.createElement("thead");
    const headRow = document.createElement("tr");
    headRow.innerHTML = "<th>Price</th>" + shown.map((c) => {
      const d = new Date(c.ts * 1000);
      const label = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      const deltaClass = c.delta >= 0 ? "pos" : "neg";
      return `<th>${label}<br><span class="${deltaClass}">Δ${fmtSignedQty(c.delta)}</span></th>`;
    }).join("");
    thead.appendChild(headRow);
    table.appendChild(thead);

    const tbody = document.createElement("tbody");
    for (const price of prices) {
      const tr = document.createElement("tr");
      const priceTd = document.createElement("td");
      priceTd.className = "price-col";
      priceTd.textContent = price.toFixed(2);
      tr.appendChild(priceTd);

      for (const c of shown) {
        const td = document.createElement("td");
        const row = c.rows.find((r) => Math.abs(r.price - price) < 1e-6);
        const classes = ["cell"];
        if (c.poc !== null && Math.abs(c.poc - price) < 1e-6) classes.push("poc");
        if (c.value_area && price <= c.value_area[1] && price >= c.value_area[0]) classes.push("value-area");
        if (c.close_row !== null && Math.abs(c.close_row - price) < 1e-6) {
          classes.push(c.bullish ? "close-bull" : "close-bear");
        }
        td.className = classes.join(" ");
        const buy = row ? row.buy : 0;
        const sell = row ? row.sell : 0;
        // Vtrender layout: sell (left) then buy (right), with only the
        // dominant side of each cell colored (green=buy dominates,
        // red=sell dominates, per its Glossary) rather than a fixed
        // buy-is-always-green / sell-is-always-red column coloring.
        const sellClass = sell > buy ? "cell-sell" : "";
        const buyClass = buy > sell ? "cell-buy" : "";
        td.innerHTML = `<span class="${sellClass}">${fmtQty(sell)}</span>` +
          `<span class="cell-sep"> </span><span class="${buyClass}">${fmtQty(buy)}</span>`;
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
  }

  function renderFootprintPanel(fp, fmtQty, fmtSignedQty, ids = {}) {
    const { cvdElId = "of-cvd", deltaElId = "of-delta", badgeWrapId = "of-imbalances",
            canvasId = "cvd-canvas", tableId = "fp-table" } = ids;
    if (!fp) return;

    const cvd = fp.candles.length ? fp.candles[fp.candles.length - 1].cvd : 0;
    const lastDelta = fp.candles.length ? fp.candles[fp.candles.length - 1].delta : 0;
    const cvdEl = $(cvdElId);
    cvdEl.textContent = fmtSignedQty(cvd ?? 0);
    cvdEl.className = (cvd || 0) >= 0 ? "pos" : "neg";
    const deltaEl = $(deltaElId);
    deltaEl.textContent = fmtSignedQty(lastDelta);
    deltaEl.className = lastDelta >= 0 ? "pos" : "neg";

    const badgeWrap = $(badgeWrapId);
    badgeWrap.innerHTML = "";
    for (const imb of fp.imbalances) {
      const b = document.createElement("span");
      b.className = "badge " + (imb.kind === "BUY_IMBALANCE" ? "buy" : "sell");
      b.textContent = `${imb.kind.replace("_IMBALANCE", "")} STACK @ ${imb.prices.map((p) => Number(p).toFixed(2)).join(",")}`;
      badgeWrap.appendChild(b);
    }

    drawCvdSparkline(fp.candles, canvasId);
    buildFootprintTable(fp.candles, fmtQty, fmtSignedQty, tableId);
  }

  // Populates a <select id=selectId> from GET /api/contracts and POSTs a
  // chosen one to /api/contract on change. Shared by app.js and
  // orderflow.js (both live pages; /replay switches SAVED SESSIONS instead,
  // a different control). `onSwitched(contract|null)` fires after every
  // switch attempt (success or failure) so a caller can update its own
  // status text; failures are surfaced via a plain `alert`, mirroring how
  // this codebase already reports the goto-time-parse and lot-size-input
  // errors elsewhere (no toast/notification system exists yet).
  async function initContractSwitcher(selectId, onSwitched) {
    const select = $(selectId);
    if (!select) return;
    let loading = false;

    async function load() {
      loading = true;
      try {
        const data = await (await fetch("/api/contracts")).json();
        const prevValue = select.value;
        select.innerHTML = "";
        for (const c of data.contracts || []) {
          const opt = document.createElement("option");
          opt.value = c.token != null ? String(c.token) : c.tradingsymbol;
          opt.textContent = c.tradingsymbol + (c.expiry ? ` (${c.expiry})` : "");
          if (c.active) opt.selected = true;
          select.appendChild(opt);
        }
        if (!data.contracts?.some((c) => c.active) && prevValue) select.value = prevValue;
      } catch {
        // leave whatever options are already there; a live broadcast still
        // shows the true active symbol even if this list can't refresh
      } finally {
        loading = false;
      }
    }

    select.addEventListener("change", async () => {
      if (loading) return;
      const chosen = select.value;
      select.disabled = true;
      try {
        const body = { token: chosen };
        const res = await (await fetch("/api/contract", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
        })).json();
        if (!res.ok) {
          alert("Contract switch failed: " + (res.error || "unknown error"));
          if (onSwitched) onSwitched(null);
        } else if (onSwitched) {
          onSwitched(res.contract);
        }
      } catch (err) {
        alert("Contract switch failed: " + err.message);
        if (onSwitched) onSwitched(null);
      } finally {
        select.disabled = false;
        await load();   // resync: shows whichever contract is ACTUALLY active now
      }
    });

    await load();
  }

  window.OFRender = {
    $, fmt, fmtInt, makeQtyFormatters, renderOrderBook, renderFootprintPanel, initContractSwitcher,
  };
})();

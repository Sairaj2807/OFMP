(() => {
  const { $, fmt, fmtInt, makeQtyFormatters, renderOrderBook, renderFootprintPanel } = window.OFRender;

  const ALLOWED_INTERVALS = ["60", "180", "300", "900", "1800"];

  let unitMode = localStorage.getItem("unitMode") === "lots" ? "lots" : "qty";
  let lotSize = 1;
  const { fmtQty, fmtSignedQty } = makeQtyFormatters(() => unitMode, () => lotSize);

  let pprValue = Math.min(5, Math.max(1, parseInt(localStorage.getItem("pprValue"), 10) || 1));
  let intervalValue = ALLOWED_INTERVALS.includes(localStorage.getItem("intervalValue"))
    ? Number(localStorage.getItem("intervalValue")) : 60;

  let currentDate = null;
  let sessionMeta = null;   // {start_ms, end_ms, trade_count, tick_size}
  let cursorMs = null;
  let lastSnapshot = null;
  let playTimer = null;
  let fetchSeq = 0;         // guards against out-of-order responses while scrubbing fast

  function setUnitMode(mode) {
    unitMode = mode;
    localStorage.setItem("unitMode", mode);
    $("unit-qty").classList.toggle("active", mode === "qty");
    $("unit-lots").classList.toggle("active", mode === "lots");
    if (lastSnapshot) renderSnapshot(lastSnapshot);
  }

  function setStatus(text, ok) {
    $("status-text").textContent = text;
    $("status-dot").className = "dot " + (ok ? "connected" : "disconnected");
  }

  function fmtClock(ms) {
    if (ms === null || ms === undefined) return "—";
    const d = new Date(ms);
    return d.toLocaleTimeString([], { hour12: false }) + "." + String(d.getMilliseconds()).padStart(3, "0");
  }

  function fmtGap(ms) {
    if (ms === null || ms === undefined) return "";
    return (ms / 1000).toFixed(2) + "s";
  }

  async function loadSessions() {
    const res = await fetch("/api/replay/sessions");
    const data = await res.json();
    const select = $("session-select");
    select.innerHTML = "";
    if (!data.sessions || !data.sessions.length) {
      select.innerHTML = "<option value=''>— no sessions logged yet —</option>";
      setStatus("no saved sessions", false);
      return;
    }
    for (const date of data.sessions) {
      const opt = document.createElement("option");
      opt.value = date;
      opt.textContent = date;
      select.appendChild(opt);
    }
    // default to the most recent session
    select.value = data.sessions[data.sessions.length - 1];
    await selectSession(select.value);
  }

  async function selectSession(date) {
    stopPlayback();
    currentDate = date;
    if (!date) return;
    const res = await fetch(`/api/replay/${date}/meta`);
    const meta = await res.json();
    if (!meta.found) {
      setStatus(`no trades logged for ${date}`, false);
      return;
    }
    sessionMeta = meta;
    if (meta.lot_size) lotSize = meta.lot_size;

    $("session-meta").textContent =
      `${meta.trade_count} trades · ${fmtClock(meta.start_ms)} → ${fmtClock(meta.end_ms)} · ` +
      `tick ${meta.tick_size}${meta.lot_size ? ` · lot ${meta.lot_size}` : ""}`;

    const slider = $("rp-slider");
    slider.min = meta.start_ms;
    slider.max = meta.end_ms;
    slider.disabled = false;
    ["rp-prev", "rp-next", "rp-play"].forEach((id) => { $(id).disabled = false; });

    cursorMs = meta.start_ms;
    slider.value = cursorMs;
    setStatus(`replaying ${date}`, true);
    await fetchAndRender();
  }

  async function fetchAndRender() {
    if (!currentDate || cursorMs === null) return;
    const seq = ++fetchSeq;
    const url = `/api/replay/${currentDate}/snapshot?as_of_ms=${cursorMs}&ppr=${pprValue}&interval=${intervalValue}`;
    const res = await fetch(url);
    const snap = await res.json();
    if (seq !== fetchSeq) return; // a newer request already landed
    if (!snap.ready) return;
    lastSnapshot = snap;
    renderSnapshot(snap);
  }

  function renderSnapshot(snap) {
    renderOrderBook(snap.book, fmtQty);
    renderFootprintPanel(snap.footprint, fmtQty, fmtSignedQty);
    $("rp-slider").value = snap.session.cursor_ms;
    $("rp-cursor-time").textContent = fmtClock(snap.session.cursor_ms);

    const t = snap.cursor_trade;
    const card = $("rp-trade-card");
    if (!t) {
      card.textContent = "No trade at or before this cursor yet.";
    } else {
      card.innerHTML =
        `<strong>#${t.trade_id}</strong> · ${fmtClock(t.ts_ms)} · ` +
        `<span class="${t.algo_side === "BUY" ? "pos" : "neg"}">${t.algo_side}</span> ` +
        `${fmtQty(t.qty)} @ ${fmt(t.ltp)} <span class="dim">(${t.algo_reason || ""})</span>`;
    }

    const gapHint = $("rp-gap-hint");
    if (snap.next_trade && snap.gap_to_next_ms !== null) {
      gapHint.textContent =
        `→ advance Vtrender by +${fmtGap(snap.gap_to_next_ms)} to next trade at ${fmtClock(snap.next_trade.ts_ms)} ` +
        `(${snap.next_trade.algo_side} ${fmtQty(snap.next_trade.qty)} @ ${fmt(snap.next_trade.ltp)})`;
    } else {
      gapHint.textContent = snap.cursor_trade ? "— last trade of the session —" : "";
    }
  }

  function stepToTrade(which) {
    if (!lastSnapshot) return;
    const target = lastSnapshot[which]; // "prev_trade" | "next_trade"
    if (!target) return;
    stopPlayback();
    cursorMs = target.ts_ms;
    fetchAndRender();
  }

  function stopPlayback() {
    if (playTimer) {
      clearInterval(playTimer);
      playTimer = null;
    }
    $("rp-play").textContent = "▶ Play";
  }

  function togglePlayback() {
    if (playTimer) {
      stopPlayback();
      return;
    }
    if (!sessionMeta) return;
    const speed = parseFloat($("rp-speed").value) || 1;
    const tickMs = 250;
    $("rp-play").textContent = "⏸ Pause";
    playTimer = setInterval(() => {
      cursorMs += tickMs * speed;
      if (cursorMs >= sessionMeta.end_ms) {
        cursorMs = sessionMeta.end_ms;
        stopPlayback();
      }
      fetchAndRender();
    }, tickMs);
  }

  function gotoTypedTime() {
    if (!currentDate) return;
    const raw = $("rp-goto").value.trim();
    const m = raw.match(/^(\d{1,2}):(\d{2})(?::(\d{2})(?:\.(\d{1,3}))?)?$/);
    if (!m) {
      $("rp-goto").classList.add("invalid");
      return;
    }
    $("rp-goto").classList.remove("invalid");
    const [, hh, mm, ss = "0", ms = "0"] = m;
    const d = new Date(currentDate + "T00:00:00");
    d.setHours(Number(hh), Number(mm), Number(ss), Number(ms.padEnd(3, "0")));
    stopPlayback();
    cursorMs = Math.min(Math.max(d.getTime(), sessionMeta.start_ms), sessionMeta.end_ms);
    fetchAndRender();
  }

  // Debounced so dragging the slider doesn't fire a request per pixel.
  let sliderDebounce = null;
  function onSliderInput(e) {
    stopPlayback();
    cursorMs = Number(e.target.value);
    $("rp-cursor-time").textContent = fmtClock(cursorMs);
    clearTimeout(sliderDebounce);
    sliderDebounce = setTimeout(fetchAndRender, 80);
  }

  $("session-select").addEventListener("change", (e) => selectSession(e.target.value));
  $("rp-slider").addEventListener("input", onSliderInput);
  $("rp-play").addEventListener("click", togglePlayback);
  $("rp-prev").addEventListener("click", () => stepToTrade("prev_trade"));
  $("rp-next").addEventListener("click", () => stepToTrade("next_trade"));
  $("rp-goto-btn").addEventListener("click", gotoTypedTime);
  $("rp-goto").addEventListener("keydown", (e) => { if (e.key === "Enter") gotoTypedTime(); });

  $("unit-qty").addEventListener("click", () => setUnitMode("qty"));
  $("unit-lots").addEventListener("click", () => setUnitMode("lots"));
  $("unit-qty").classList.toggle("active", unitMode === "qty");
  $("unit-lots").classList.toggle("active", unitMode === "lots");

  $("ppr-select").value = String(pprValue);
  $("ppr-select").addEventListener("change", (e) => {
    pprValue = Math.min(5, Math.max(1, parseInt(e.target.value, 10) || 1));
    localStorage.setItem("pprValue", String(pprValue));
    if (lastSnapshot) fetchAndRender();
  });

  $("interval-select").value = String(intervalValue);
  $("interval-select").addEventListener("change", (e) => {
    intervalValue = ALLOWED_INTERVALS.includes(e.target.value) ? Number(e.target.value) : 60;
    localStorage.setItem("intervalValue", String(intervalValue));
    if (lastSnapshot) fetchAndRender();
  });

  loadSessions();
})();

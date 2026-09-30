(() => {
  const { $, fmt, fmtInt, makeQtyFormatters, renderOrderBook, renderFootprintPanel, initContractSwitcher } = window.OFRender;

  const ALLOWED_INTERVALS = ["60", "180", "300", "900", "1800"];

  let unitMode = localStorage.getItem("unitMode") === "lots" ? "lots" : "qty";
  let lotSize = 1;
  let lastSnapshot = null;
  let currentWs = null;
  let pprValue = Math.min(5, Math.max(1, parseInt(localStorage.getItem("pprValue"), 10) || 1));
  let intervalValue = ALLOWED_INTERVALS.includes(localStorage.getItem("intervalValue"))
    ? Number(localStorage.getItem("intervalValue")) : 60;

  const { fmtQty, fmtSignedQty } = makeQtyFormatters(() => unitMode, () => lotSize);

  function sendState() {
    if (currentWs && currentWs.readyState === WebSocket.OPEN) {
      currentWs.send(JSON.stringify({ ppr: pprValue, interval: intervalValue }));
    }
  }

  function setUnitMode(mode) {
    unitMode = mode;
    localStorage.setItem("unitMode", mode);
    $("unit-qty").classList.toggle("active", mode === "qty");
    $("unit-lots").classList.toggle("active", mode === "lots");
    if (lastSnapshot) render(lastSnapshot);
  }

  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/frontend`);
    currentWs = ws;

    ws.onopen = () => { setStatusDot(true); sendState(); };
    ws.onclose = () => { setStatusDot(false); setTimeout(connect, 2000); };
    ws.onerror = () => ws.close();
    ws.onmessage = (evt) => {
      const snap = JSON.parse(evt.data);
      if (!snap.ready) return;
      lastSnapshot = snap;
      render(snap);
    };
  }

  function setStatusDot(wsOpen) {
    const dot = $("status-dot");
    dot.className = "dot " + (wsOpen ? "connected" : "disconnected");
    $("status-text").textContent = wsOpen ? "streaming" : "reconnecting…";
  }

  function render(snap) {
    renderStatus(snap.status);
    renderQuote(snap.quote);
    renderOrderBook(snap.book, fmtQty);
    renderFootprintPanel(snap.footprint, fmtQty, fmtSignedQty);
  }

  function renderStatus(status) {
    if (!status) return;
    $("symbol").textContent = status.symbol || "—";
    $("q-ticks").textContent = fmtInt(status.tick_count);
    if (status.lot_size) lotSize = status.lot_size;
    const dot = $("status-dot");
    if (status.connected) {
      dot.className = "dot connected";
      $("status-text").textContent = "feed connected";
    } else {
      dot.className = "dot disconnected";
      $("status-text").textContent = status.error ? `error: ${status.error}` : "feed disconnected";
    }
  }

  function renderQuote(q) {
    if (!q) return;
    $("q-ltp").textContent = fmt(q.ltp);
    $("q-open").textContent = fmt(q.open);
    $("q-high").textContent = fmt(q.high);
    $("q-low").textContent = fmt(q.low);
    $("q-close").textContent = fmt(q.close);
    $("q-oi").textContent = fmtQty(q.oi);
    $("q-vol").textContent = fmtQty(q.volume);
  }

  $("unit-qty").addEventListener("click", () => setUnitMode("qty"));
  $("unit-lots").addEventListener("click", () => setUnitMode("lots"));
  $("unit-qty").classList.toggle("active", unitMode === "qty");
  $("unit-lots").classList.toggle("active", unitMode === "lots");

  $("ppr-select").value = String(pprValue);
  $("ppr-select").addEventListener("change", (e) => {
    pprValue = Math.min(5, Math.max(1, parseInt(e.target.value, 10) || 1));
    localStorage.setItem("pprValue", String(pprValue));
    sendState();
  });

  $("interval-select").value = String(intervalValue);
  $("interval-select").addEventListener("change", (e) => {
    intervalValue = ALLOWED_INTERVALS.includes(e.target.value) ? Number(e.target.value) : 60;
    localStorage.setItem("intervalValue", String(intervalValue));
    sendState();
  });

  // A switch resets the backend's engine (fresh footprint/CVD) but the
  // /ws/frontend socket itself stays open — the next broadcast (within
  // BROADCAST_INTERVAL_SEC) already reflects the new contract via
  // renderStatus, so there's nothing else to wire up here.
  initContractSwitcher("contract-select");

  connect();
})();

"""server.py's contract switcher: _activate_contract, _on_tick's per-record
symbol tagging, and contract listing / switching through the gateway the
/api/v1 layer uses (POST /api/v1/admin/contract). A fake ws
feed stands in for the live feed (server.LiveFeed) so nothing here
touches the network."""
import asyncio

import pytest

import server
from backend.app.domain.market_data import DepthLevel, MarketTick


def run(coro):
    return asyncio.run(coro)


class FakeWsClient:
    """Mirrors the live feeds' shape (run()/stop()) but
    never opens a real connection -- run() just blocks until cancelled, the
    same way the real clients block inside their own reconnect loop."""
    instances = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.stopped = False
        self.started = False
        FakeWsClient.instances.append(self)

    def stop(self):
        self.stopped = True

    async def run(self):
        self.started = True
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise


@pytest.fixture(autouse=True)
def fake_clients(monkeypatch):
    FakeWsClient.instances = []
    monkeypatch.setattr(server, "LiveFeed", FakeWsClient)
    yield
    # tear down any task _activate_contract left running so tests don't leak
    task = getattr(server.app.state, "ws_task", None)
    if task and not task.done():
        task.cancel()


def contract(symbol="NIFTY24SEP26FUT", token="111", tick_size=0.1, lotsize=65):
    return {"token": token, "tradingsymbol": symbol, "tick_size": tick_size,
            "lotsize": lotsize, "exch_seg": "NFO"}


# ---- _activate_contract ---------------------------------------------------------

def test_activate_contract_sets_state_and_starts_the_client():
    run(server._activate_contract(contract("NIFTY24SEP26FUT")))
    assert server.STATE["contract"]["tradingsymbol"] == "NIFTY24SEP26FUT"
    assert server.STATE["engine"] is not None and server.STATE["engine"].tick_count == 0
    assert server.STATE["connected"] is False and server.STATE["last_error"] is None
    assert len(FakeWsClient.instances) == 1 and FakeWsClient.instances[0].started


def test_activate_contract_replaces_the_engine_and_stops_the_old_client():
    run(server._activate_contract(contract("NIFTY24SEP26FUT")))
    old_engine, old_client = server.STATE["engine"], server.STATE["ws_client"]
    old_engine.tick_count = 42   # prove it's really a DIFFERENT object afterward, not reused

    run(server._activate_contract(contract("NIFTY29OCT26FUT", token="222")))
    assert server.STATE["contract"]["tradingsymbol"] == "NIFTY29OCT26FUT"
    assert server.STATE["engine"] is not old_engine
    assert server.STATE["engine"].tick_count == 0          # a fresh footprint/CVD, not carried over
    assert old_client.stopped is True
    assert len(FakeWsClient.instances) == 2


def test_activate_contract_cancels_the_old_ws_task():
    """Both switches run inside ONE asyncio.run() call, and every check on
    task liveness happens before that call returns: asyncio.run() cancels
    any still-pending task as part of its own shutdown, so checking a task's
    .done() state from OUTSIDE the run() that created it can't tell a task
    the code genuinely left running apart from one asyncio.run() swept up on
    exit -- both would read back as "done" by then, for unrelated reasons."""
    async def both():
        await server._activate_contract(contract())
        old_task = server.app.state.ws_task
        await server._activate_contract(contract("NIFTY29OCT26FUT", token="222"))
        new_task = server.app.state.ws_task
        await asyncio.sleep(0)   # let the new task actually start running
        return old_task.done(), new_task is old_task, new_task.done(), FakeWsClient.instances[-1].started
    old_done, replaced, new_done, new_started = run(both())
    assert old_done is True
    assert replaced is False
    assert new_done is False
    assert new_started is True


def test_two_switches_fired_concurrently_leave_self_consistent_state():
    """Regression guard, not a proof CONTRACT_SWITCH_LOCK is load-bearing:
    checked by mutation (temporarily removing the lock) and this specific
    scenario still passed, because _activate_contract's STATE-mutating block
    has no `await` inside it -- Python's cooperative scheduling can only
    interleave two coroutines AT an await point, so "last writer wins"
    cleanly either way here. The lock stays in as a real safeguard should
    that ever change (e.g. an async step added to client teardown, or a
    real client whose constructor/stop() does I/O, unlike FakeWsClient's
    synchronous stop()) -- this test only pins down that the END state is
    self-consistent, which holds regardless of whether the lock fires."""
    async def both():
        await asyncio.gather(
            server._activate_contract(contract("A", token="1", tick_size=0.1)),
            server._activate_contract(contract("B", token="2", tick_size=0.05)),
        )
    run(both())
    running = [c for c in FakeWsClient.instances if not c.stopped]
    assert len(running) == 1                                   # exactly one client left live
    # never mismatched: engine's footprint always keyed to whichever contract STATE settled on
    assert server.STATE["engine"].footprint.tick_size == server.STATE["contract"]["tick_size"]


# ---- _on_tick symbol tagging ------------------------------------------------------

class FakeStore:
    def __init__(self):
        self.logged = []

    def log(self, obs):
        self.logged.append(obs)
        return len(self.logged)


def make_tick(ltp=100.0, ltt=1_700_000_000_000, cum_volume=65):
    return MarketTick(provider="angelone", token="111", exchange_segment="NFO", sequence=None,
                      exchange_ts_ms=ltt, last_trade_ts_ms=ltt, received_ts_ms=ltt, ltp=ltp,
                      last_traded_qty=None, cumulative_volume=cum_volume,
                      bids=(DepthLevel(99.9, 65, 1),), asks=(DepthLevel(100.1, 65, 1),))


def test_on_tick_tags_the_active_symbol():
    run(server._activate_contract(contract("NIFTY24SEP26FUT")))
    store = FakeStore()
    server.STATE["observation_store"] = store
    server._on_tick(make_tick(cum_volume=0))    # primes the counter, no trade yet
    server._on_tick(make_tick(cum_volume=65))   # now a trade
    assert store.logged and store.logged[-1]["symbol"] == "NIFTY24SEP26FUT"


def test_on_tick_tags_correctly_across_a_mid_stream_switch():
    """Two trades either side of a live switch must each carry the symbol
    that was ACTUALLY active when they were logged, not the final one."""
    run(server._activate_contract(contract("NIFTY24SEP26FUT")))
    store = FakeStore()
    server.STATE["observation_store"] = store
    server._on_tick(make_tick(cum_volume=0))
    server._on_tick(make_tick(cum_volume=65))            # trade under September

    run(server._activate_contract(contract("NIFTY29OCT26FUT", token="222")))
    server._on_tick(make_tick(cum_volume=0))
    server._on_tick(make_tick(cum_volume=65))            # trade under October

    symbols = [o["symbol"] for o in store.logged]
    assert symbols == ["NIFTY24SEP26FUT", "NIFTY29OCT26FUT"]


def test_live_trades_feed_the_market_profile_and_a_switch_starts_a_new_one():
    run(server._activate_contract(contract("NIFTY24SEP26FUT")))
    profile = server.STATE["engine"].profile
    t0 = 1_790_655_000_000                                   # 2026-09-29 09:40 IST: inside the NSE session
    server._on_tick(make_tick(ltt=t0, cum_volume=0))
    server._on_tick(make_tick(ltp=100.0, ltt=t0 + 1000, cum_volume=65))
    server._on_tick(make_tick(ltp=100.5, ltt=t0 + 2000, cum_volume=195))
    assert profile.trades == 2 and profile.date == "2026-09-29"
    snap = server._build_profile_snapshot(1)
    assert snap["ready"] and snap["view"] == "profile" and snap["mode"] == "live"
    assert snap["profile"]["stats"]["volume"] == 195 and snap["profile"]["rows"][0]["letters"] == "A"
    run(server._activate_contract(contract("NIFTY29OCT26FUT", token="222")))
    assert server.STATE["engine"].profile is not profile and server.STATE["engine"].profile.trades == 0
    assert server._build_profile_snapshot(5)["profile"] == {"date": None, "rows": [], "empty": True}


def test_on_tick_without_a_store_configured_does_not_crash():
    run(server._activate_contract(contract()))
    server.STATE["observation_store"] = None
    server._on_tick(make_tick(cum_volume=0))
    server._on_tick(make_tick(cum_volume=65))   # must not raise


# ---- listing and switching contracts ----------------------------------------------

def fake_angel_rows():
    return [
        {"token": "1", "tradingsymbol": "NIFTY24SEP26FUT", "name": "NIFTY", "expiry": "24SEP2026",
         "tick_size": 0.1, "lotsize": 65, "exch_seg": "NFO"},
        {"token": "2", "tradingsymbol": "NIFTY29OCT26FUT", "name": "NIFTY", "expiry": "29OCT2026",
         "tick_size": 0.1, "lotsize": 65, "exch_seg": "NFO"},
    ]


def test_gateway_lists_the_switchable_contracts(monkeypatch):
    import angel_client
    monkeypatch.setattr(angel_client, "list_configured_futures", lambda **k: fake_angel_rows())
    rows = run(server.ServerMarketGateway().list_contracts())
    assert [r["tradingsymbol"] for r in rows] == ["NIFTY24SEP26FUT", "NIFTY29OCT26FUT"]


def test_gateway_switch_contract_valid_token(monkeypatch):
    import angel_client
    monkeypatch.setattr(angel_client, "list_configured_futures", lambda **k: fake_angel_rows())
    run(server._activate_contract(contract("NIFTY24SEP26FUT", token="1")))

    switched = run(server.ServerMarketGateway().switch_contract("2"))
    assert switched["tradingsymbol"] == "NIFTY29OCT26FUT"
    assert server.STATE["contract"]["tradingsymbol"] == "NIFTY29OCT26FUT"


def test_gateway_switch_contract_unknown_token_leaves_state_untouched(monkeypatch):
    import angel_client
    monkeypatch.setattr(angel_client, "list_configured_futures", lambda **k: fake_angel_rows())
    run(server._activate_contract(contract("NIFTY24SEP26FUT", token="1")))

    with pytest.raises(ValueError, match="999"):
        run(server.ServerMarketGateway().switch_contract("999"))
    assert server.STATE["contract"]["tradingsymbol"] == "NIFTY24SEP26FUT"   # unchanged


# ---- LiveFeed selection -------------------------------------------------------------

def test_live_feed_follows_ingest_mode(monkeypatch):
    from backend.app.services.market_data.feeds import EmbeddedFeed, RedisFeed
    monkeypatch.undo()                      # use the real LiveFeed, not the fixture's fake
    import config
    c = contract()
    monkeypatch.setattr(config, "INGEST_MODE", "embedded")
    monkeypatch.setattr(config, "RECORD_RAW_TICKS", False)
    feed = server.LiveFeed(c, on_tick=lambda t: None, on_status=lambda **kw: None)
    assert isinstance(feed, EmbeddedFeed) and feed.recorder is None
    assert feed.instrument.token == "111" and feed.instrument.exchange_segment == "NFO"
    monkeypatch.setattr(config, "INGEST_MODE", "redis")
    assert isinstance(server.LiveFeed(c, on_tick=lambda t: None, on_status=lambda **kw: None), RedisFeed)

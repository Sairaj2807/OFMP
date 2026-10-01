"""Server-side ReplaySession: virtual-clock playback, seek/step, and parity
with the one-shot rebuild (replay_engine.build_replay_state) — the replay a
viewer streams must equal the state the engine would have at that instant."""
import pytest

import orderbook_engine as oe
import replay_engine
import server
from backend.app.services.replay import ReplayLibrary, ReplaySession
from helpers import TICK, synthetic_ticks


@pytest.fixture(scope="module")
def records():
    eng, out = oe.TickProcessorState(TICK), []
    for t in synthetic_ticks(n_ticks=1500, seed=5):
        oe.process_tick(eng, t, observation_sink=out.append)
    for i, r in enumerate(out):
        r["trade_id"] = i + 1
    return out


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def payload(state):
    return server._chart_payload(state.footprint, state.cvd_tracker.cvd, state.last_candle_seen, 1, limit=10_000)


def test_seek_forward_and_backward_match_the_one_shot_rebuild(records):
    s = ReplaySession("2026-09-29", records, TICK, clock=Clock())
    for target in (records[len(records) // 2]["ts_ms"], records[-1]["ts_ms"], records[100]["ts_ms"], records[900]["ts_ms"]):
        s.seek(target)
        expected = replay_engine.build_replay_state(records, TICK, target)
        assert payload(s.state) == payload(expected), target
        assert s.state.book.top_n(5) == expected.book.top_n(5)
        assert s.index == expected.trade_count


def test_profile_follows_seek_forward_and_backward(records):
    """The replay's market profile at any cursor equals one built fresh from the
    trades up to that cursor (seeking back rebuilds; nothing leaks across)."""
    from backend.app.domain.profile import NSE_SESSION, SessionProfile, build_profile
    s = ReplaySession("2026-09-29", records, TICK, clock=Clock())
    for target in (records[-1]["ts_ms"], records[300]["ts_ms"], records[1000]["ts_ms"], records[10]["ts_ms"]):
        s.seek(target)
        fresh = SessionProfile(TICK, NSE_SESSION)
        for r in records:
            if r["ts_ms"] > target:
                break
            fresh.add_trade(r["ts_ms"], r["ltp"], r["qty"], r["algo_side"])
        assert build_profile(s.state.profile, 5) == build_profile(fresh, 5), target


def test_play_advances_by_elapsed_time_times_speed_and_pause_freezes(records):
    clock = Clock()
    s = ReplaySession("d", records, TICK, clock=clock)
    s.set_speed(10)
    s.play()
    start = s.cursor_ms
    clock.t += 2.0                                   # 2 s real time at 10x
    s.advance()
    assert s.cursor_ms == start + 20_000
    s.pause()
    clock.t += 5.0
    s.advance()
    assert s.cursor_ms == start + 20_000 and not s.playing


def test_playback_stops_at_the_end_and_play_restarts(records):
    clock = Clock()
    s = ReplaySession("d", records, TICK, clock=clock)
    s.set_speed(50)
    s.play()
    clock.t += 10**6
    s.advance()
    assert s.index == len(records) and not s.playing and s.cursor_ms == s.end_ms
    s.play()                                         # at the end: start over
    assert s.index == 0 and s.playing


def test_step_trade_and_candle(records):
    s = ReplaySession("d", records, TICK, clock=Clock())
    s.step("trade")
    first_ts = records[0]["ts_ms"]
    assert s.cursor_ms == first_ts and s.index == sum(1 for r in records if r["ts_ms"] == first_ts)
    s.step("candle")
    assert (s.cursor_ms + 1) % 60_000 == 0            # parked on a candle boundary
    assert all(r["ts_ms"] <= s.cursor_ms for r in records[: s.index])
    assert s.index == len(records) or records[s.index]["ts_ms"] > s.cursor_ms
    with pytest.raises(ValueError):
        s.step("week")


def test_speed_validation_and_meta(records):
    s = ReplaySession("2026-09-29", records, TICK, clock=Clock())
    with pytest.raises(ValueError):
        s.set_speed(3)
    m = s.meta()
    assert (m["date"], m["total"], m["index"], m["speeds"]) == ("2026-09-29", len(records), 0, [1, 2, 5, 10, 25, 50])
    with pytest.raises(ValueError):
        ReplaySession("d", [], TICK)


def test_library_caches_and_evicts():
    loads = []

    async def loader(d):
        loads.append(d)
        return [{"ts_ms": 1, "ltp": 1.0, "qty": 1, "algo_side": "BUY"}]

    async def lister():
        return []

    import asyncio
    lib = ReplayLibrary(loader, lister, max_cached=2)

    async def go():
        for d in ("a", "b", "a", "c", "b"):
            await lib.records(d)
    asyncio.run(go())
    assert loads == ["a", "b", "c", "b"]             # "a" served from cache once; "b" evicted by "c"

"""Read/write queries used outside the hot path (startup restore, instrument
sync, imports, reconciliation)."""
from datetime import date, datetime
from typing import Optional

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from .rows import dt_to_ms
from .schema import instruments, raw_ticks, trades


async def load_session_trades(engine: AsyncEngine, provider: str, symbol: str, session_date: date) -> list:
    """One contract's trades for one session, oldest first, as observation
    dicts (the stored features record, with trade_id)."""
    stmt = (sa.select(trades.c.features, trades.c.trade_seq, trades.c.trade_ts)
            .where(trades.c.provider == provider, trades.c.symbol == symbol,
                   trades.c.session_date == session_date)
            .order_by(trades.c.trade_ts, trades.c.trade_seq))
    async with engine.connect() as conn:
        rows = (await conn.execute(stmt)).all()
    out = []
    for features, seq, ts in rows:
        obs = dict(features)
        obs["trade_id"] = seq
        obs["ts_ms"] = dt_to_ms(ts)
        out.append(obs)
    return out


def _parse_expiry(expiry) -> Optional[date]:
    """Scrip-master expiry string, e.g. "27OCT2026" -> date; None if absent/unparseable."""
    try:
        return datetime.strptime(str(expiry).upper(), "%d%b%Y").date()
    except (TypeError, ValueError):
        return None


async def upsert_instrument(engine: AsyncEngine, contract: dict, provider: str = "angelone",
                            instrument_type: Optional[str] = None) -> None:
    """Insert or refresh one instrument from a resolved contract dict
    (angel_client._contract_dict shape)."""
    values = {
        "provider": provider, "token": str(contract["token"]),
        "exchange_segment": contract.get("exch_seg", "NFO"), "symbol": contract["tradingsymbol"],
        "name": contract.get("name"), "instrument_type": instrument_type,
        "expiry": _parse_expiry(contract.get("expiry")), "lot_size": int(contract["lotsize"]),
        "tick_size": contract["tick_size"],
    }
    stmt = insert(instruments).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["provider", "token"],
        set_={k: stmt.excluded[k] for k in values if k not in ("provider", "token")} | {"updated_at": sa.func.now()})
    async with engine.begin() as conn:
        await conn.execute(stmt)


async def trade_counts_by_session(engine: AsyncEngine, provider: Optional[str] = None) -> dict:
    """{session_date: trade count}."""
    stmt = sa.select(trades.c.session_date, sa.func.count()).group_by(trades.c.session_date)
    if provider:
        stmt = stmt.where(trades.c.provider == provider)
    async with engine.connect() as conn:
        return {d: n for d, n in (await conn.execute(stmt)).all()}


async def previous_session_date(engine: AsyncEngine, provider: str, symbol: str, before: date) -> Optional[date]:
    """The latest session before `before` with stored trades of `symbol` (None if there is none)."""
    stmt = sa.select(sa.func.max(trades.c.session_date)).where(
        trades.c.provider == provider, trades.c.symbol == symbol, trades.c.session_date < before)
    async with engine.connect() as conn:
        return (await conn.execute(stmt)).scalar_one_or_none()


async def raw_tick_count(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return (await conn.execute(sa.select(sa.func.count()).select_from(raw_ticks))).scalar_one()


async def load_session_tape(engine: AsyncEngine, provider: str, symbol: str, session_date: date) -> tuple:
    """What restoring a live session needs, without the heavy features column:
    ([(ts_ms, price, qty, side), ...] oldest first, last trade's (bid_levels, ask_levels))."""
    base = (trades.c.provider == provider, trades.c.symbol == symbol, trades.c.session_date == session_date)
    tape_stmt = (sa.select(trades.c.trade_ts, trades.c.price, trades.c.qty, trades.c.side)
                 .where(*base).order_by(trades.c.trade_ts, trades.c.trade_seq))
    last_stmt = (sa.select(trades.c.features["bid_levels"], trades.c.features["ask_levels"])
                 .where(*base).order_by(trades.c.trade_ts.desc(), trades.c.trade_seq.desc()).limit(1))
    async with engine.connect() as conn:
        tape = [(dt_to_ms(ts), price, qty, side) for ts, price, qty, side in (await conn.execute(tape_stmt)).all()]
        last = (await conn.execute(last_stmt)).first()
    return tape, (tuple(last) if last else (None, None))


async def load_trades_by_date(engine: AsyncEngine, provider: str, session_date: date) -> list:
    """Every stored trade of one session, oldest first, as observation dicts
    (features + trade_id + ts_ms) — the input replay needs. Includes sessions
    recorded before trades were tagged with a symbol."""
    stmt = (sa.select(trades.c.features, trades.c.trade_seq, trades.c.trade_ts)
            .where(trades.c.provider == provider, trades.c.session_date == session_date)
            .order_by(trades.c.trade_ts, trades.c.trade_seq))
    async with engine.connect() as conn:
        rows = (await conn.execute(stmt)).all()
    out = []
    for features, seq, ts in rows:
        obs = dict(features)
        obs["trade_id"] = seq
        obs["ts_ms"] = dt_to_ms(ts)
        out.append(obs)
    return out

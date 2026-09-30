"""Domain objects <-> table rows. Pure functions, no I/O."""
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from backend.app.domain.market_data import DataQualityEvent, MarketTick

IST = timezone(timedelta(hours=5, minutes=30))


def ms_to_dt(ms: Optional[int]) -> Optional[datetime]:
    return datetime.fromtimestamp(ms / 1000, timezone.utc) if ms is not None else None


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    return int(round(dt.timestamp() * 1000)) if dt is not None else None


def ist_date(ms: int) -> date:
    """IST trading date of an epoch-ms timestamp (NSE has no DST)."""
    return datetime.fromtimestamp(ms / 1000, IST).date()


def tick_row(t: MarketTick) -> dict:
    return {
        "received_at": ms_to_dt(t.received_ts_ms),
        "provider": t.provider,
        "token": t.token,
        "sequence": t.sequence if t.sequence is not None else 0,
        "exchange_ts": ms_to_dt(t.exchange_ts_ms),
        "last_trade_ts": ms_to_dt(t.last_trade_ts_ms),
        "ltp": t.ltp,
        "last_traded_qty": t.last_traded_qty,
        "cumulative_volume": t.cumulative_volume,
        "bid_px": [lv.price for lv in t.bids],
        "bid_qty": [lv.qty for lv in t.bids],
        "bid_orders": [lv.orders for lv in t.bids],
        "ask_px": [lv.price for lv in t.asks],
        "ask_qty": [lv.qty for lv in t.asks],
        "ask_orders": [lv.orders for lv in t.asks],
        "exchange_segment": t.exchange_segment,
        "avg_price": t.avg_price,
        "open": t.open,
        "high": t.high,
        "low": t.low,
        "close": t.close,
        "open_interest": t.open_interest,
        "total_buy_qty": t.total_buy_qty,
        "total_sell_qty": t.total_sell_qty,
        "extra": t.extra or None,
    }


def trade_row(obs: dict, provider: str, token: Optional[str] = None,
              session_date: Optional[date] = None) -> dict:
    """An observation record (as written to observations.jsonl, trade_id set)
    -> trades row. Records from before classifier versioning carry no
    classifier fields; they were all produced by vtrender_reconstruction/v1."""
    return {
        "trade_ts": ms_to_dt(obs["ts_ms"]),
        "provider": provider,
        "session_date": session_date or ist_date(obs["ts_ms"]),
        "trade_seq": obs["trade_id"],
        "symbol": obs.get("symbol"),
        "token": token,
        "price": obs["ltp"],
        "qty": obs["qty"],
        "side": obs["algo_side"],
        "method": obs.get("algo_reason"),
        "classifier_name": obs.get("classifier_name", "vtrender_reconstruction"),
        "classifier_version": obs.get("classifier_version", "v1"),
        "best_bid": obs.get("best_bid"),
        "best_ask": obs.get("best_ask"),
        "cumulative_volume": obs.get("cum_volume"),
        "features": obs,
    }


def quality_event_row(e: DataQualityEvent) -> dict:
    return {"detected_at": ms_to_dt(e.ts_ms), "provider": e.provider, "token": e.token,
            "kind": e.kind, "severity": e.severity, "detail": e.detail}

"""Angel One SmartAPI WebSocket 2.0 Snap Quote (mode 3) binary packet ->
MarketTick. Layout per the WS 2.0 response contract; little-endian; prices
in paise."""
import struct

from backend.app.domain.market_data import DepthLevel, MarketTick

PROVIDER = "angelone"
SNAP_QUOTE_MIN_LEN = 147 + 200   # header + 10 x 20-byte best-five sub-packets


def _parse_best_five(buf: bytes, offset: int, count: int = 10):
    """20-byte sub-packets: flag(int16), qty(int64), price(int64, paise), orders(int16).
    flag 1 = buy side. Returned in packet order (best first)."""
    buys, sells = [], []
    for i in range(count):
        flag, qty, price_paise, orders = struct.unpack_from("<hqqh", buf, offset + i * 20)
        level = DepthLevel(price_paise / 100.0, qty, orders)
        (buys if flag == 1 else sells).append(level)
    return tuple(buys), tuple(sells)


def parse_snap_quote(buf: bytes, received_ts_ms: int, exchange_segment: str = None) -> MarketTick:
    """Raises struct.error on a truncated packet."""
    mode, exch_type = struct.unpack_from("<bb", buf, 0)
    token = buf[2:27].split(b"\x00", 1)[0].decode("utf-8", errors="ignore")
    seq_no, exch_ts = struct.unpack_from("<qq", buf, 27)
    ltp_paise, ltq, avg_price_paise, volume = struct.unpack_from("<qqqq", buf, 43)
    tot_buy_qty, tot_sell_qty = struct.unpack_from("<dd", buf, 75)
    open_p, high_p, low_p, close_p = struct.unpack_from("<qqqq", buf, 91)
    ltt, oi = struct.unpack_from("<qq", buf, 123)
    bids, asks = _parse_best_five(buf, 147)

    return MarketTick(
        provider=PROVIDER,
        token=token,
        exchange_segment=exchange_segment,
        sequence=seq_no,
        exchange_ts_ms=exch_ts,
        last_trade_ts_ms=ltt * 1000 if ltt < 10_000_000_000 else ltt,   # seconds -> ms if needed
        received_ts_ms=received_ts_ms,
        ltp=ltp_paise / 100.0,
        last_traded_qty=ltq,
        cumulative_volume=volume,
        bids=bids,
        asks=asks,
        avg_price=avg_price_paise / 100.0,
        open=open_p / 100.0,
        high=high_p / 100.0,
        low=low_p / 100.0,
        close=close_p / 100.0,
        open_interest=oi,
        total_buy_qty=tot_buy_qty,
        total_sell_qty=tot_sell_qty,
        extra={"mode": mode, "exchange_type": exch_type},
    )

"""Footprint aggregation: trades bucketed into native candles x price levels."""
import math
from typing import Optional

from .analytics import poc_from_rows, stacked_imbalances_from_rows, value_area_from_rows
from .models import FootprintCell, FootprintRow, Trade
from .settings import IMBALANCE_THRESHOLD, MIN_STACK, VALUE_AREA_PCT


class Footprint:
    def __init__(self, tick_size: float, candle_interval_sec: int):
        self.tick_size = tick_size
        self.candle_interval = candle_interval_sec
        self.data: dict = {}          # candle_ts -> {price: FootprintCell}
        self.candle_ohlc: dict = {}   # candle_ts -> {"open","high","low","close"}
        # candle_ts -> {"run","min","max","trades"}: the candle's running delta
        # in trade order (initial zero included) plus the number of Trade
        # objects added. Min/max cannot be recovered from the aggregated
        # cells afterwards, so they have to be tracked as trades arrive. The
        # order-flow chart's min/max-delta rows are built from this.
        self.candle_stats: dict = {}

    def _bucket_price(self, price: float) -> float:
        return round(round(price / self.tick_size) * self.tick_size, 8)

    def _bucket_time(self, ts_millis: int) -> int:
        secs = ts_millis // 1000
        return secs - (secs % self.candle_interval)

    def row_price_for(self, price: float, ppr: int = 1) -> float:
        """The PPR-bucketed row a raw price falls into (see get_candle_rows)."""
        width = max(ppr, self.tick_size)
        return round(math.floor(price / width) * width, 8)

    def add_trade(self, trade: Trade) -> int:
        """Returns the candle timestamp the trade was bucketed into."""
        candle_ts = self._bucket_time(trade.timestamp)
        price_lvl = self._bucket_price(trade.price)
        candle = self.data.setdefault(candle_ts, {})
        cell = candle.setdefault(price_lvl, FootprintCell())
        if trade.side == "BUY":
            cell.buy_volume += trade.quantity
            cell.buy_trades += 1
        else:
            cell.sell_volume += trade.quantity
            cell.sell_trades += 1

        stats = self.candle_stats.get(candle_ts)
        if stats is None:
            stats = self.candle_stats[candle_ts] = {"run": 0, "min": 0, "max": 0, "trades": 0}
        stats["run"] += trade.quantity if trade.side == "BUY" else -trade.quantity
        if stats["run"] < stats["min"]:
            stats["min"] = stats["run"]
        if stats["run"] > stats["max"]:
            stats["max"] = stats["run"]
        stats["trades"] += 1

        ohlc = self.candle_ohlc.get(candle_ts)
        if ohlc is None:
            self.candle_ohlc[candle_ts] = {
                "open": trade.price, "high": trade.price,
                "low": trade.price, "close": trade.price,
            }
        else:
            ohlc["high"] = max(ohlc["high"], trade.price)
            ohlc["low"] = min(ohlc["low"], trade.price)
            ohlc["close"] = trade.price

        return candle_ts

    def candle_delta(self, candle_ts) -> int:
        return sum(cell.delta for cell in self.data.get(candle_ts, {}).values())

    def get_candle_rows(self, candle_ts, ppr: int = 1) -> list:
        """This candle's footprint as price-descending rows. `ppr` (price per
        row) is the price WIDTH of one row in rupees — e.g. ppr=1 folds every
        traded price within the same whole-rupee band (24384.00..24384.99)
        into one row labeled by the band's floor (24384.90 + 24384.00 -> 24384)."""
        candle = self.data.get(candle_ts, {})
        if not candle:
            return []

        grouped: dict = {}
        for price, cell in candle.items():
            row_price = self.row_price_for(price, ppr)
            g = grouped.setdefault(row_price, FootprintCell())
            g.buy_volume += cell.buy_volume
            g.sell_volume += cell.sell_volume
            g.buy_trades += cell.buy_trades
            g.sell_trades += cell.sell_trades

        rows = [
            FootprintRow(
                price=price,
                buy_volume=c.buy_volume,
                sell_volume=c.sell_volume,
                buy_trades=c.buy_trades,
                sell_trades=c.sell_trades,
                delta=c.delta,
            )
            for price, c in grouped.items()
        ]
        rows.sort(key=lambda r: r.price, reverse=True)
        return rows

    def poc(self, candle_ts, ppr: int = 1) -> Optional[float]:
        return poc_from_rows(self.get_candle_rows(candle_ts, ppr))

    def value_area(self, candle_ts, target_pct=VALUE_AREA_PCT, ppr: int = 1):
        rows = self.get_candle_rows(candle_ts, ppr)
        return value_area_from_rows(rows, poc_from_rows(rows), target_pct)

    def stacked_imbalances(self, candle_ts, threshold=IMBALANCE_THRESHOLD,
                            min_stack=MIN_STACK, ppr: int = 1):
        return stacked_imbalances_from_rows(self.get_candle_rows(candle_ts, ppr), threshold, min_stack)

    def merged_candle_rows(self, candle_ts_list: list, ppr: int = 1) -> list:
        """Several native candles' rows (see get_candle_rows), summed by price
        into one price-descending list — how a coarser display interval's POC/
        value-area/imbalances get computed without a second bucketing scheme:
        poc_from_rows/value_area_from_rows/stacked_imbalances_from_rows run
        on this exactly as they do on a single native candle's own rows, so
        native and grouped views share the same analytics code, not a
        parallel reimplementation of it. See group_candle_timestamps, which
        decides which native candles belong in one `candle_ts_list`."""
        merged: dict = {}
        for ts in candle_ts_list:
            for row in self.get_candle_rows(ts, ppr):
                m = merged.get(row.price)
                if m is None:
                    m = merged[row.price] = FootprintRow(
                        price=row.price, buy_volume=0, sell_volume=0,
                        buy_trades=0, sell_trades=0, delta=0)
                m.buy_volume += row.buy_volume
                m.sell_volume += row.sell_volume
                m.buy_trades += row.buy_trades
                m.sell_trades += row.sell_trades
                m.delta = m.buy_volume - m.sell_volume
        rows = list(merged.values())
        rows.sort(key=lambda r: r.price, reverse=True)
        return rows

    def prune_old(self, keep_last: int):
        if len(self.data) <= keep_last:
            return
        for ts in sorted(self.data.keys())[:-keep_last]:
            del self.data[ts]
            self.candle_stats.pop(ts, None)
            self.candle_ohlc.pop(ts, None)

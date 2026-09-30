"""Builds Angel One WS 2.0 Snap Quote (mode 3) binary packets for tests,
following the same layout the parser reads."""
import random
import struct


def pack_snap_quote(token="12345", seq=1, exch_ts_ms=1_790_653_500_123, ltp=24000.0, ltq=65,
                    avg=23990.5, volume=100_000, tot_buy=1e6, tot_sell=2e6,
                    ohlc=(23900.0, 24100.0, 23850.0, 23950.0), ltt_s=1_790_653_500, oi=1_234_567,
                    bids=((23999.9, 130, 2),), asks=((24000.1, 65, 1),), exch_type=2):
    paise = lambda p: int(round(p * 100))
    buf = bytearray(347)
    struct.pack_into("<bb", buf, 0, 3, exch_type)
    buf[2:27] = token.encode().ljust(25, b"\x00")
    struct.pack_into("<qq", buf, 27, seq, exch_ts_ms)
    struct.pack_into("<qqqq", buf, 43, paise(ltp), ltq, paise(avg), volume)
    struct.pack_into("<dd", buf, 75, tot_buy, tot_sell)
    struct.pack_into("<qqqq", buf, 91, *(paise(x) for x in ohlc))
    struct.pack_into("<qq", buf, 123, ltt_s, oi)
    levels = [(1, b) for b in bids][:5] + [(0, a) for a in asks][:5]
    levels += [(1, (0.0, 0, 0))] * (5 - min(5, len(bids))) + [(0, (0.0, 0, 0))] * (5 - min(5, len(asks)))
    for i, (flag, (price, qty, orders)) in enumerate(levels):
        struct.pack_into("<hqqh", buf, 147 + i * 20, flag, qty, paise(price), orders)
    return bytes(buf)


def random_snap_quote(rng: random.Random, seq: int):
    mid = round(24000 + rng.randint(-500, 500) * 0.1, 1)
    bids = tuple((round(mid - 0.1 * (k + 1), 1), rng.randint(1, 50) * 65, rng.randint(1, 9)) for k in range(5))
    asks = tuple((round(mid + 0.1 * (k + 1), 1), rng.randint(1, 50) * 65, rng.randint(1, 9)) for k in range(5))
    return pack_snap_quote(token=str(rng.randint(1000, 99999)), seq=seq, exch_ts_ms=1_790_653_500_000 + seq * 250,
                           ltp=mid, ltq=rng.randint(1, 20) * 65, volume=seq * 650, ltt_s=1_790_653_500 + seq // 4,
                           bids=bids, asks=asks, oi=rng.randint(0, 10**7))

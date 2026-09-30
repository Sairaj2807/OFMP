"""REST-side helpers: token lifecycle, instrument master, contract resolution.

Tokens never leave this process — callers get back a resolved contract dict
and a short-lived in-memory token, nothing is ever forwarded to the browser.
"""
import base64
import json
import os
import time
from datetime import date
from typing import Optional

import requests

import angelone_autologin as autologin
import config

_HEADERS_STATIC = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "X-UserType": "USER",
    "X-SourceID": "WEB",
    "X-ClientLocalIP": None,
    "X-ClientPublicIP": None,
    "X-MACAddress": "00:00:00:00:00:00",
    "X-PrivateKey": autologin.API_KEY,
}


def _rest_headers(jwt_token: str) -> dict:
    h = dict(_HEADERS_STATIC)
    h["X-ClientLocalIP"] = autologin.get_local_ip()
    h["X-ClientPublicIP"] = autologin.get_public_ip()
    h["Authorization"] = f"Bearer {jwt_token}"
    return h


def load_tokens() -> dict:
    tokens = {}
    if not os.path.exists(config.TOKENS_FILE):
        return tokens
    with open(config.TOKENS_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line or "=" not in line:
                continue
            k, v = line.split("=", 1)
            tokens[k.strip()] = v.strip()
    return tokens


def save_tokens(jwt_token: str, refresh_token: str, feed_token: str) -> None:
    with open(config.TOKENS_FILE, "w") as f:
        f.write(f"jwt_token={jwt_token}\n")
        f.write(f"refresh_token={refresh_token}\n")
        f.write(f"feed_token={feed_token}\n")


def _decode_jwt_exp(jwt_token: str) -> int:
    """Return the `exp` claim (epoch seconds) without verifying signature."""
    try:
        payload_b64 = jwt_token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        return int(payload.get("exp", 0))
    except Exception:
        return 0


def ensure_valid_token(margin_sec: int = 120) -> dict:
    """Return a dict with jwt_token/refresh_token/feed_token that is valid for
    at least `margin_sec` more seconds, refreshing or re-logging-in as needed."""
    tokens = load_tokens()
    jwt_token = tokens.get("jwt_token", "")
    exp = _decode_jwt_exp(jwt_token) if jwt_token else 0

    if jwt_token and exp - time.time() > margin_sec:
        return tokens

    # try a lightweight refresh first
    if jwt_token and tokens.get("refresh_token"):
        try:
            data = autologin.generate_token(jwt_token, tokens["refresh_token"])
            new_jwt = data.get("jwtToken", jwt_token)
            new_refresh = data.get("refreshToken", tokens["refresh_token"])
            new_feed = data.get("feedToken", tokens.get("feed_token", ""))
            save_tokens(new_jwt, new_refresh, new_feed)
            print("[angel_client] Token refreshed via generateTokens")
            return {"jwt_token": new_jwt, "refresh_token": new_refresh, "feed_token": new_feed}
        except Exception as e:
            print(f"[angel_client] Refresh failed ({e}), falling back to full login")

    # full re-login
    login_data = autologin.login()
    new_jwt = login_data["jwtToken"]
    new_refresh = login_data["refreshToken"]
    new_feed = login_data["feedToken"]
    save_tokens(new_jwt, new_refresh, new_feed)
    print("[angel_client] Fresh login completed")
    return {"jwt_token": new_jwt, "refresh_token": new_refresh, "feed_token": new_feed}


def fetch_instrument_master(force: bool = False) -> list:
    """Download (and cache locally, once per day) the full scrip master."""
    if not force and os.path.exists(config.INSTRUMENTS_CACHE_FILE):
        mtime = os.path.getmtime(config.INSTRUMENTS_CACHE_FILE)
        if time.time() - mtime < 24 * 3600:
            with open(config.INSTRUMENTS_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)

    print("[angel_client] Downloading instrument master ...")
    resp = requests.get(config.INSTRUMENT_MASTER_URL, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    with open(config.INSTRUMENTS_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)
    print(f"[angel_client] Instrument master cached ({len(data)} rows)")
    return data


_EXPIRY_MONTH_ALIASES = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def parse_expiry_date(expiry: str) -> Optional[date]:
    """Angel expiry strings look like '28AUG2026' (two-digit day) but a
    single-digit day is a valid date too ('5AUG2026'), so this doesn't
    assume a fixed width. Returns a date, or None if the string doesn't
    parse (no recognizable month name, no digits left for a day once the
    4-digit year is taken, or an impossible date like day 32)."""
    expiry = (expiry or "").strip().upper()
    for alias, month_num in _EXPIRY_MONTH_ALIASES.items():
        if alias in expiry:
            digits = "".join(ch for ch in expiry if ch.isdigit())
            # digits = D(D)YYYY (1-2 digit day + 4-digit year) once the month letters are stripped
            if len(digits) < 5:
                return None
            try:
                return date(int(digits[-4:]), month_num, int(digits[:-4]))
            except ValueError:
                return None
    return None


def list_configured_futures(instruments: list = None, as_of: Optional[date] = None,
                            expiry_month: Optional[str] = None) -> list:
    """Every config.INSTRUMENT_NAME/INSTRUMENT_TYPE/INSTRUMENT_EXCH_SEG future
    that hasn't expired as of `as_of` (today if not given), soonest first.
    This is what makes contract resolution auto-roll: "soonest unexpired" is
    a question that's never stale, unlike "the September one" — once
    September's contract's expiry date has passed, it simply stops matching
    and October's becomes the soonest, with no code change needed month to
    month.

    `expiry_month` (e.g. "DEC") restricts the result to just that month —
    resolve_configured_future() passes config.INSTRUMENT_EXPIRY_MONTH here,
    which defaults to None ("any month, take the soonest unexpired one" —
    the auto-roll behavior); set it to pin a specific far-month contract
    instead. Passed explicitly (not read from config directly) so this also
    serves /api/contracts, which wants every upcoming month, unfiltered."""
    instruments = instruments if instruments is not None else fetch_instrument_master()
    as_of = as_of or date.today()

    candidates = []
    for row in instruments:
        if row.get("exch_seg") != config.INSTRUMENT_EXCH_SEG:
            continue
        if row.get("instrumenttype") != config.INSTRUMENT_TYPE:
            continue
        if row.get("name") != config.INSTRUMENT_NAME:
            continue
        expiry_date = parse_expiry_date(row.get("expiry", ""))
        if expiry_date is None or expiry_date < as_of:
            continue
        if expiry_month and expiry_date.month != _EXPIRY_MONTH_ALIASES[expiry_month]:
            continue
        candidates.append((expiry_date, row))

    candidates.sort(key=lambda c: c[0])
    return [_contract_dict(row) for _, row in candidates]


def _contract_dict(row: dict) -> dict:
    tick_size = float(row.get("tick_size", "100")) / 100.0  # paise -> rupees
    return {
        "token": row["token"],
        "tradingsymbol": row["symbol"],
        "name": row["name"],
        "expiry": row["expiry"],
        "tick_size": tick_size,
        "lotsize": int(row.get("lotsize", "1")),
        "exch_seg": row["exch_seg"],
    }


def resolve_configured_future(instruments: list = None, as_of: Optional[date] = None) -> dict:
    """The single contract to use by default: the soonest unexpired one
    matching config.INSTRUMENT_* (see list_configured_futures — this is the
    auto-roll entry point, called fresh on every server start)."""
    candidates = list_configured_futures(instruments, as_of=as_of, expiry_month=config.INSTRUMENT_EXPIRY_MONTH)
    if not candidates:
        scope = f" expiring in {config.INSTRUMENT_EXPIRY_MONTH}" if config.INSTRUMENT_EXPIRY_MONTH else ""
        raise RuntimeError(
            f"No unexpired {config.INSTRUMENT_NAME} {config.INSTRUMENT_TYPE} contract found on "
            f"{config.INSTRUMENT_EXCH_SEG}{scope}"
        )
    return candidates[0]


if __name__ == "__main__":
    toks = ensure_valid_token()
    print("Token OK, expires at epoch", _decode_jwt_exp(toks["jwt_token"]))
    contract = resolve_configured_future()
    print("Resolved contract:", contract)

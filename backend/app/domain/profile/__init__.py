"""Market Profile (TPO + volume at price) per session. See engine.py for the definitions."""
from .analytics import VALUE_AREA_PCT, build_profile, poc_index, value_area
from .engine import ALL_DAY_SESSION, LETTERS, NSE_SESSION, ProfileSession, SessionProfile, letter

ROW_SIZES = (1, 2, 5, 10, 20, 50)        # profile row sizes offered, in points
DEFAULT_ROW = 5
# Bump when the profile maths or its output changes: cached historical profiles are keyed by it.
PROFILE_VERSION = "profile-v2"     # v2: VWAP/TWAP, HVN bands (70% of VPOC), IB extensions, DPOC, weak H/L
MAX_HISTORY_SESSIONS = 50

__all__ = ["ALL_DAY_SESSION", "DEFAULT_ROW", "LETTERS", "MAX_HISTORY_SESSIONS", "NSE_SESSION", "PROFILE_VERSION",
           "ProfileSession", "ROW_SIZES",
           "SessionProfile", "VALUE_AREA_PCT", "build_profile", "letter", "poc_index", "value_area"]

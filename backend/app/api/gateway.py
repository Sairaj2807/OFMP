"""What the API layer needs from the market-data side, as an interface, so
routes and the stream hub never import the application module that owns the
live engine."""
from typing import Optional, Protocol


class MarketGateway(Protocol):
    def active_contract(self) -> Optional[dict]:
        """The live contract (token, tradingsymbol, tick_size, lotsize, ...)."""

    async def profile_levels(self, row: float, composite: Optional[str]) -> dict:
        """Naked POCs and an optional composite profile from finished sessions."""

    def broker_status(self) -> Optional[dict]:
        """The daily broker session (None when not scheduled: synthetic feed, worker mode)."""

    def broker_relogin(self) -> bool:
        """Log in again now (admins); False when there is no broker session."""

    def feed_status(self) -> Optional[dict]:
        """Provider/feed health, including "connected"."""

    def database_stats(self) -> Optional[dict]:
        """Database writer stats, or None when persistence is off."""

    async def list_contracts(self) -> list:
        """Contracts available to switch to."""

    async def switch_contract(self, token: str) -> dict:
        """Make the contract with this token the live one; returns it. Raises ValueError if unknown."""

    def chart_snapshot(self, ppr: int, interval_sec: int) -> dict:
        """The live order-flow chart snapshot for these display settings."""

    def profile_snapshot(self, row: float) -> dict:
        """The live market profile snapshot (data.view == "profile") at this row size, in points."""

    def history_epoch(self) -> tuple:
        """(active symbol, live session date): changes mean historical profiles must be re-sent."""

    async def profile_history(self, row: float, limit: int, date_from: Optional[str] = None,
                              date_to: Optional[str] = None) -> dict:
        """Finished sessions of the active contract, each an independent profile, oldest first."""

    async def footprint_sessions(self, interval_sec: int) -> dict:
        """Finished sessions of the active contract available as footprint history (capped by interval)."""

    async def footprint_day(self, day: str, ppr: int, interval_sec: int) -> Optional[dict]:
        """One session as footprint bars: finished (cached) or today (live engine). None if unavailable."""

    async def session_profile(self, date: str, row: float) -> Optional[dict]:
        """A stored session's whole market profile (None if nothing is stored for that date)."""

    def replay_profile_snapshot(self, session, row: float) -> dict:
        """A market profile snapshot of the replay's current position (with data.replay meta)."""

    async def replay_sessions(self) -> list:
        """[{date, trades}] of stored sessions available for replay."""

    async def open_replay(self, date: str):
        """A ReplaySession for that date (raises if nothing is stored)."""

    def replay_snapshot(self, session, ppr: int, interval_sec: int) -> dict:
        """A chart snapshot of the replay's current position (with data.replay meta)."""

"""What the API layer needs from the market-data side, as an interface, so
routes and the stream hub never import the application module that owns the
live engine."""
from typing import Optional, Protocol


class MarketGateway(Protocol):
    def active_contract(self) -> Optional[dict]:
        """The live contract (token, tradingsymbol, tick_size, lotsize, ...)."""

    def feed_status(self) -> Optional[dict]:
        """Provider/feed health, including "connected"."""

    def database_stats(self) -> Optional[dict]:
        """Database writer stats, or None when persistence is off."""

    async def list_contracts(self) -> list:
        """Contracts available to switch to."""

    def chart_snapshot(self, ppr: int, interval_sec: int) -> dict:
        """The live order-flow chart snapshot for these display settings."""

    async def replay_sessions(self) -> list:
        """[{date, trades}] of stored sessions available for replay."""

    async def open_replay(self, date: str):
        """A ReplaySession for that date (raises if nothing is stored)."""

    def replay_snapshot(self, session, ppr: int, interval_sec: int) -> dict:
        """A chart snapshot of the replay's current position (with data.replay meta)."""

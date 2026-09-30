"""Market-data provider interface.

A provider owns one connection to one broker/vendor and turns its wire
format into MarketTicks. It does not reconnect by itself: ProviderRunner
supervises it (connect, subscribe, stream, and on failure disconnect, back
off, reconnect, resubscribe). Adding a broker means implementing this class;
nothing downstream changes."""
from abc import ABC, abstractmethod
from typing import AsyncIterator, Iterable

from backend.app.domain.market_data import InstrumentRef, MarketTick


class MarketDataProvider(ABC):
    name: str = "provider"

    @abstractmethod
    async def connect(self) -> None:
        """Open the connection (authenticating if needed). Raises on failure."""

    @abstractmethod
    async def subscribe(self, instruments: Iterable[InstrumentRef]) -> None:
        """Start receiving ticks for these instruments on the open connection."""

    @abstractmethod
    async def unsubscribe(self, instruments: Iterable[InstrumentRef]) -> None:
        """Stop receiving ticks for these instruments."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Close the connection. Must be safe to call when not connected."""

    @abstractmethod
    def stream(self) -> AsyncIterator[MarketTick]:
        """Yield normalized ticks until the connection ends (return) or fails
        (raise). Malformed packets are skipped and counted via on_drop."""

    def on_message(self, received_ts_ms: int) -> None:
        """Hook the runner sets to learn about every frame (heartbeats too)."""

    def on_drop(self, reason: str) -> None:
        """Hook the runner sets to count packets that could not be parsed."""

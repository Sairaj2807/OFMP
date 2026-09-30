"""Provider connection health."""
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Optional


class ProviderState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"
    DEGRADED = "DEGRADED"        # connected, but no data for longer than expected
    STOPPED = "STOPPED"


@dataclass
class ProviderHealth:
    provider: str
    state: ProviderState = ProviderState.DISCONNECTED
    last_tick_ms: Optional[int] = None         # server receive time of the last tick
    last_message_ms: Optional[int] = None      # any frame, including pongs
    last_connect_ms: Optional[int] = None
    last_disconnect_ms: Optional[int] = None
    last_error: Optional[str] = None
    reconnects: int = 0
    subscriptions: list = field(default_factory=list)   # tokens
    ticks_received: int = 0
    ticks_dropped: int = 0          # malformed / unparseable packets
    ticks_failed: int = 0           # parsed, but the downstream consumer raised
    quality_events: int = 0

    @property
    def connected(self) -> bool:
        return self.state in (ProviderState.CONNECTED, ProviderState.DEGRADED)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["state"] = self.state.value
        d["connected"] = self.connected
        return d

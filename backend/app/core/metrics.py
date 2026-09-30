"""Prometheus metrics (scraped from /metrics on the API, and from the ingest
worker's own port). Names are prefixed ofmp_.

Latency note: ofmp_tick_provider_latency_seconds is server receive time minus
the provider-reported exchange timestamp. It measures provider -> server
delay only as well as the provider's clock allows; it is NOT verified
exchange latency.

The API runs a single process (the engine lives in it), so the default
in-process registry is correct; multi-process uvicorn would need
prometheus_client's multiprocess mode."""
import time
from typing import Callable

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

TICKS_RECEIVED = Counter("ofmp_ticks_received_total", "Normalized ticks received from the provider", ["provider"])
TICKS_DROPPED = Counter("ofmp_ticks_dropped_total", "Provider packets that could not be parsed", ["provider"])
TICKS_FAILED = Counter("ofmp_ticks_failed_total", "Ticks whose consumer raised (engine, publish)", ["provider"])
PROVIDER_RECONNECTS = Counter("ofmp_provider_reconnects_total", "Provider reconnect attempts", ["provider"])
PROVIDER_CONNECTED = Gauge("ofmp_provider_connected", "1 while the provider connection is up", ["provider"])
QUALITY_EVENTS = Counter("ofmp_data_quality_events_total", "Data-quality findings", ["kind", "severity"])
TICK_PROVIDER_LATENCY = Histogram(
    "ofmp_tick_provider_latency_seconds", "Server receive time minus provider exchange timestamp", ["provider"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10))
LAST_TICK_TIME = Gauge("ofmp_last_tick_timestamp_seconds", "Server receive time of the latest tick", ["provider"])

TICK_PROCESSING = Histogram(
    "ofmp_tick_processing_seconds", "Engine time to process one tick (book, classification, footprint)",
    buckets=(0.00005, 0.0001, 0.00025, 0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05))
TRADES_CLASSIFIED = Counter("ofmp_trades_classified_total", "Trades classified by the engine",
                            ["classifier", "version", "side"])

WS_CONNECTIONS = Gauge("ofmp_ws_connections", "Open /ws/v1/stream connections")
WS_SUBSCRIPTIONS = Gauge("ofmp_ws_subscriptions", "Active stream subscriptions")
WS_MESSAGES = Counter("ofmp_ws_messages_sent_total", "Messages sent on /ws/v1/stream", ["type"])
WS_REJECTED = Counter("ofmp_ws_rejected_total", "Refused /ws/v1/stream connections", ["reason"])
WS_COALESCED = Counter("ofmp_ws_snapshots_coalesced_total", "Snapshots replaced before a slow client read them")

DB_ROWS_WRITTEN = Counter("ofmp_db_rows_written_total", "Rows written by the batched writer", ["table"])
DB_ROWS_DROPPED = Counter("ofmp_db_rows_dropped_total", "Rows dropped because the writer buffer was full", ["table"])
DB_ROWS_PENDING = Gauge("ofmp_db_rows_pending", "Rows buffered, waiting to be written", ["table"])
DB_WRITE_ERRORS = Counter("ofmp_db_write_errors_total", "Failed writer flushes")

HTTP_REQUESTS = Counter("ofmp_http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_LATENCY = Histogram("ofmp_http_request_duration_seconds", "HTTP request latency", ["method", "route"],
                         buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5))


def render() -> tuple:
    """(body, content type) for a /metrics response."""
    return generate_latest(), CONTENT_TYPE_LATEST


def timed(histogram: Histogram) -> Callable:
    """Context manager-free timing helper: returns stop() that observes elapsed seconds."""
    start = time.perf_counter()
    return lambda: histogram.observe(time.perf_counter() - start)

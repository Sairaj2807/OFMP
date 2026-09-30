# Monitoring

## Components

- **Prometheus** (`deploy/prometheus/`) scrapes:
  - `api:8000/metrics`
  - `worker:9108/metrics`

  Retention is 30 days, and alert rules are in `alerts.yml`.
- **Grafana** (`deploy/grafana/`) is provisioned with the Prometheus datasource and the **OFMP —
  platform overview** dashboard. It binds to localhost:

  ```bash
  ssh -L 3002:127.0.0.1:3002 server
  ```

  then open http://127.0.0.1:3002.
- **Logs:**
  - The API and worker write JSON lines with `request_id` and `user_id`; secrets are redacted.
  - nginx writes JSON access logs carrying the same `request_id`, which it forwards as `X-Request-ID`.
  - Docker rotates log files: 20 MB × 5.
- **Health:**
  - `/live`: the process is up.
  - `/ready`: the database is reachable. Returns 503 if it is not.
  - `/health`: summary.
  - Container healthchecks gate startup order.

`/metrics` is internal only: nginx returns 404 for it from outside.

## Metrics

| Metric | Meaning |
|---|---|
| `ofmp_ticks_received_total{provider}` | normalized ticks received |
| `ofmp_ticks_dropped_total` / `ofmp_ticks_failed_total` | unparseable packets / consumer (engine, publish) exceptions |
| `ofmp_provider_connected`, `ofmp_provider_reconnects_total` | broker connection state |
| `ofmp_last_tick_timestamp_seconds` | receive time of the latest tick (staleness) |
| `ofmp_tick_provider_latency_seconds` | receive time minus provider timestamp. This is the **provider's claim, not verified exchange latency**. |
| `ofmp_tick_processing_seconds` | engine time per tick |
| `ofmp_trades_classified_total{classifier,version,side}` | classification volume per algorithm version |
| `ofmp_data_quality_events_total{kind,severity}` | sequence, timestamp, volume and book anomalies |
| `ofmp_ws_connections`, `ofmp_ws_subscriptions`, `ofmp_ws_messages_sent_total`, `ofmp_ws_snapshots_coalesced_total`, `ofmp_ws_rejected_total{reason}` | real-time stream |
| `ofmp_db_rows_written_total`, `ofmp_db_rows_pending`, `ofmp_db_rows_dropped_total`, `ofmp_db_write_errors_total` | batched database writer |
| `ofmp_http_requests_total{method,route,status}`, `ofmp_http_request_duration_seconds` | API. Labelled by route template, so the label set stays bounded. |

## Alerts

`deploy/prometheus/alerts.yml`, validated with `promtool`:

| Alert | Fires when |
|---|---|
| TargetDown | a scrape target is down |
| ProviderDisconnected | the provider is disconnected during NSE hours |
| NoTicksDuringMarketHours | no ticks for more than 2 minutes during NSE hours |
| TickConsumerFailures | ticks are failing in the engine or publisher |
| DatabaseWriterBacklog | the writer has more than 50k rows pending |
| DatabaseRowsDropped | the writer buffer overflowed and dropped rows |
| HighApiErrorRate | more than 5% of API requests return 5xx |
| SlowTickProcessing | p99 tick processing is above 10 ms |

**Market hours** are approximated in UTC (weekdays, about 03:00–10:00 UTC). Exchange holidays need
the exchange calendar (not yet built), so expect a spurious page on NSE holidays until then.

**Alert delivery** (Alertmanager to email, Telegram or a webhook) is configured per deployment and is
not bundled.

## Performance targets

These are engineering targets to measure against, not guarantees:
- tick processing p99 < 10 ms
- API p95 < 300 ms for chart queries
- WebSocket connect < 2 s

Load testing (hundreds of concurrent WebSocket clients) is still to be done. Measure against these
dashboards before scaling anything.

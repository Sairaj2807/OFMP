"""Relational schema (SQLAlchemy Core). The Alembic migrations create these
tables; application code queries them through these definitions.

Time-series tables (raw_ticks, trades) are TimescaleDB hypertables
partitioned by time. They deliberately have no foreign keys to instruments:
a FK check on every tick insert costs ingestion throughput and would reject
ticks for an instrument not yet synced from the scrip master. Instruments
are joined on (provider, token) / symbol at query time instead.

All timestamps are timestamptz (UTC); session_date is the IST trading date.
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

metadata = sa.MetaData(naming_convention={
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
})

NOW = sa.text("now()")

instruments = sa.Table(
    "instruments", metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("provider", sa.Text, nullable=False),
    sa.Column("token", sa.Text, nullable=False),
    sa.Column("exchange_segment", sa.Text, nullable=False),
    sa.Column("symbol", sa.Text, nullable=False),
    sa.Column("name", sa.Text),                       # underlying, e.g. NIFTY
    sa.Column("instrument_type", sa.Text),            # e.g. FUTIDX
    sa.Column("expiry", sa.Date),
    sa.Column("strike", sa.Numeric(14, 4)),
    sa.Column("option_type", sa.Text),                # CE / PE / NULL
    sa.Column("lot_size", sa.Integer, nullable=False),
    sa.Column("tick_size", sa.Numeric(12, 4), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    sa.UniqueConstraint("provider", "token"),
    sa.CheckConstraint("lot_size > 0", name="lot_size_positive"),
    sa.CheckConstraint("tick_size > 0", name="tick_size_positive"),
    sa.Index(None, "name", "instrument_type", "expiry"),
    sa.Index(None, "symbol"),
)

algorithm_versions = sa.Table(
    "algorithm_versions", metadata,
    sa.Column("id", sa.Integer, sa.Identity(), primary_key=True),
    sa.Column("kind", sa.Text, nullable=False),       # "classifier" | "engine"
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("version", sa.Text, nullable=False),
    sa.Column("description", sa.Text),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    sa.UniqueConstraint("kind", "name", "version"),
)

raw_ticks = sa.Table(
    "raw_ticks", metadata,
    sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("provider", sa.Text, nullable=False),
    sa.Column("token", sa.Text, nullable=False),
    sa.Column("sequence", sa.BigInteger, nullable=False),     # 0 when the provider sends none
    sa.Column("exchange_ts", sa.DateTime(timezone=True)),
    sa.Column("last_trade_ts", sa.DateTime(timezone=True), nullable=False),
    sa.Column("ltp", sa.Float, nullable=False),
    sa.Column("last_traded_qty", sa.BigInteger),
    sa.Column("cumulative_volume", sa.BigInteger, nullable=False),
    sa.Column("bid_px", ARRAY(sa.Float), nullable=False),
    sa.Column("bid_qty", ARRAY(sa.BigInteger), nullable=False),
    sa.Column("bid_orders", ARRAY(sa.Integer), nullable=False),
    sa.Column("ask_px", ARRAY(sa.Float), nullable=False),
    sa.Column("ask_qty", ARRAY(sa.BigInteger), nullable=False),
    sa.Column("ask_orders", ARRAY(sa.Integer), nullable=False),
    sa.Column("exchange_segment", sa.Text),
    sa.Column("avg_price", sa.Float),
    sa.Column("open", sa.Float),
    sa.Column("high", sa.Float),
    sa.Column("low", sa.Float),
    sa.Column("close", sa.Float),
    sa.Column("open_interest", sa.BigInteger),
    sa.Column("total_buy_qty", sa.Float),
    sa.Column("total_sell_qty", sa.Float),
    sa.Column("extra", JSONB),
    # Idempotency key (a re-import of the same tick is a no-op). It also serves
    # the main query: one instrument over a time range.
    sa.UniqueConstraint("provider", "token", "received_at", "sequence"),
)

trades = sa.Table(
    "trades", metadata,
    sa.Column("trade_ts", sa.DateTime(timezone=True), nullable=False),   # exchange last-trade time
    sa.Column("provider", sa.Text, nullable=False),
    sa.Column("session_date", sa.Date, nullable=False),
    sa.Column("trade_seq", sa.Integer, nullable=False),    # per-day id, = observations.jsonl trade_id
    sa.Column("symbol", sa.Text),                          # NULL for sessions recorded before tagging
    sa.Column("token", sa.Text),
    sa.Column("price", sa.Float, nullable=False),
    sa.Column("qty", sa.BigInteger, nullable=False),
    sa.Column("side", sa.Text, nullable=False),
    sa.Column("method", sa.Text),                          # classification rule that decided
    sa.Column("classifier_name", sa.Text, nullable=False),
    sa.Column("classifier_version", sa.Text, nullable=False),
    sa.Column("best_bid", sa.Float),
    sa.Column("best_ask", sa.Float),
    sa.Column("cumulative_volume", sa.BigInteger),
    sa.Column("features", JSONB),                          # full observation record (research)
    sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    sa.UniqueConstraint("provider", "session_date", "trade_seq", "trade_ts"),
    sa.CheckConstraint("side IN ('BUY', 'SELL')", name="side_valid"),
    sa.CheckConstraint("qty > 0", name="qty_positive"),
    sa.Index("ix_trades_symbol_trade_ts", "symbol", sa.text("trade_ts DESC")),
)

data_quality_events = sa.Table(
    "data_quality_events", metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("provider", sa.Text, nullable=False),
    sa.Column("token", sa.Text, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("severity", sa.Text, nullable=False),
    sa.Column("detail", sa.Text),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    sa.CheckConstraint("severity IN ('info', 'warning', 'error')", name="severity_valid"),
    sa.Index("ix_data_quality_events_detected_at", sa.text("detected_at DESC")),
    sa.Index("ix_data_quality_events_kind_detected_at", "kind", sa.text("detected_at DESC")),
)

HYPERTABLES = {"raw_ticks": "received_at", "trades": "trade_ts"}


# ---------------------------------------------------------------------------
# Identity, sessions, audit (migration 0002)
# ---------------------------------------------------------------------------
# Every user-owned object will carry owner_user_id and organization_id; an
# individual user gets a personal organization at registration, so the
# multi-tenant shape exists from day one.

organizations = sa.Table(
    "organizations", metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("is_personal", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    sa.Column("deleted_at", sa.DateTime(timezone=True)),
)

users = sa.Table(
    "users", metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("email", sa.Text, nullable=False),                 # stored lower-cased
    sa.Column("password_hash", sa.Text, nullable=False),         # Argon2id
    sa.Column("display_name", sa.Text),
    sa.Column("role", sa.Text, nullable=False, server_default="user"),
    sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column("email_verified_at", sa.DateTime(timezone=True)),
    sa.Column("last_login_at", sa.DateTime(timezone=True)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    sa.Column("deleted_at", sa.DateTime(timezone=True)),
    sa.CheckConstraint("role IN ('user', 'support', 'admin', 'super_admin')", name="role_valid"),
    sa.CheckConstraint("email = lower(email)", name="email_lowercase"),
    sa.Index("uq_users_email_active", "email", unique=True, postgresql_where=sa.text("deleted_at IS NULL")),
)

organization_members = sa.Table(
    "organization_members", metadata,
    sa.Column("organization_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"),
              primary_key=True),
    sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("role", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    sa.CheckConstraint("role IN ('owner', 'admin', 'member')", name="role_valid"),
    sa.Index(None, "user_id"),
)

user_sessions = sa.Table(
    "user_sessions", metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("revoked_at", sa.DateTime(timezone=True)),
    sa.Column("revoked_reason", sa.Text),
    sa.Column("user_agent", sa.Text),
    sa.Column("ip", sa.Text),
    sa.Index(None, "user_id"),
)

refresh_tokens = sa.Table(
    "refresh_tokens", metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("session_id", UUID(as_uuid=True), sa.ForeignKey("user_sessions.id", ondelete="CASCADE"),
              nullable=False),
    sa.Column("token_hash", sa.Text, nullable=False, unique=True),   # sha256 of the token
    sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("used_at", sa.DateTime(timezone=True)),               # set on rotation
    sa.Index(None, "session_id"),
)

email_tokens = sa.Table(
    "email_tokens", metadata,
    sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
    sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    sa.Column("purpose", sa.Text, nullable=False),
    sa.Column("token_hash", sa.Text, nullable=False, unique=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("used_at", sa.DateTime(timezone=True)),
    sa.CheckConstraint("purpose IN ('verify_email', 'reset_password')", name="purpose_valid"),
    sa.Index(None, "user_id"),
)

audit_logs = sa.Table(
    "audit_logs", metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=NOW),
    sa.Column("actor_user_id", UUID(as_uuid=True)),        # no FK: the log must outlive the user
    sa.Column("action", sa.Text, nullable=False),
    sa.Column("target_type", sa.Text),
    sa.Column("target_id", sa.Text),
    sa.Column("ip", sa.Text),
    sa.Column("user_agent", sa.Text),
    sa.Column("request_id", sa.Text),
    sa.Column("details", JSONB),
    sa.Index("ix_audit_logs_occurred_at", sa.text("occurred_at DESC")),
    sa.Index("ix_audit_logs_actor_occurred_at", "actor_user_id", sa.text("occurred_at DESC")),
)

"""initial market data schema

Instruments, algorithm versions, raw ticks and trades (TimescaleDB
hypertables, 1-day chunks), data-quality events.

Revision ID: 0001
Revises: 
Create Date: 2026-09-30 14:33:24.760547

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
    op.create_table('algorithm_versions',
    sa.Column('id', sa.Integer(), sa.Identity(always=False), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('version', sa.Text(), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_algorithm_versions')),
    sa.UniqueConstraint('kind', 'name', 'version', name=op.f('uq_algorithm_versions_kind_name_version'))
    )
    op.create_table('data_quality_events',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('detected_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('provider', sa.Text(), nullable=False),
    sa.Column('token', sa.Text(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('severity', sa.Text(), nullable=False),
    sa.Column('detail', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("severity IN ('info', 'warning', 'error')", name=op.f('ck_data_quality_events_severity_valid')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_data_quality_events'))
    )
    op.create_index('ix_data_quality_events_detected_at', 'data_quality_events', [sa.literal_column('detected_at DESC')], unique=False)
    op.create_index('ix_data_quality_events_kind_detected_at', 'data_quality_events', ['kind', sa.literal_column('detected_at DESC')], unique=False)
    op.create_table('instruments',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('provider', sa.Text(), nullable=False),
    sa.Column('token', sa.Text(), nullable=False),
    sa.Column('exchange_segment', sa.Text(), nullable=False),
    sa.Column('symbol', sa.Text(), nullable=False),
    sa.Column('name', sa.Text(), nullable=True),
    sa.Column('instrument_type', sa.Text(), nullable=True),
    sa.Column('expiry', sa.Date(), nullable=True),
    sa.Column('strike', sa.Numeric(precision=14, scale=4), nullable=True),
    sa.Column('option_type', sa.Text(), nullable=True),
    sa.Column('lot_size', sa.Integer(), nullable=False),
    sa.Column('tick_size', sa.Numeric(precision=12, scale=4), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('lot_size > 0', name=op.f('ck_instruments_lot_size_positive')),
    sa.CheckConstraint('tick_size > 0', name=op.f('ck_instruments_tick_size_positive')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_instruments')),
    sa.UniqueConstraint('provider', 'token', name=op.f('uq_instruments_provider_token'))
    )
    op.create_index(op.f('ix_instruments_name_instrument_type_expiry'), 'instruments', ['name', 'instrument_type', 'expiry'], unique=False)
    op.create_index(op.f('ix_instruments_symbol'), 'instruments', ['symbol'], unique=False)
    op.create_table('raw_ticks',
    sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('provider', sa.Text(), nullable=False),
    sa.Column('token', sa.Text(), nullable=False),
    sa.Column('sequence', sa.BigInteger(), nullable=False),
    sa.Column('exchange_ts', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_trade_ts', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ltp', sa.Float(), nullable=False),
    sa.Column('last_traded_qty', sa.BigInteger(), nullable=True),
    sa.Column('cumulative_volume', sa.BigInteger(), nullable=False),
    sa.Column('bid_px', postgresql.ARRAY(sa.Float()), nullable=False),
    sa.Column('bid_qty', postgresql.ARRAY(sa.BigInteger()), nullable=False),
    sa.Column('bid_orders', postgresql.ARRAY(sa.Integer()), nullable=False),
    sa.Column('ask_px', postgresql.ARRAY(sa.Float()), nullable=False),
    sa.Column('ask_qty', postgresql.ARRAY(sa.BigInteger()), nullable=False),
    sa.Column('ask_orders', postgresql.ARRAY(sa.Integer()), nullable=False),
    sa.Column('exchange_segment', sa.Text(), nullable=True),
    sa.Column('avg_price', sa.Float(), nullable=True),
    sa.Column('open', sa.Float(), nullable=True),
    sa.Column('high', sa.Float(), nullable=True),
    sa.Column('low', sa.Float(), nullable=True),
    sa.Column('close', sa.Float(), nullable=True),
    sa.Column('open_interest', sa.BigInteger(), nullable=True),
    sa.Column('total_buy_qty', sa.Float(), nullable=True),
    sa.Column('total_sell_qty', sa.Float(), nullable=True),
    sa.Column('extra', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.UniqueConstraint('provider', 'token', 'received_at', 'sequence', name=op.f('uq_raw_ticks_provider_token_received_at_sequence'))
    )
    op.create_table('trades',
    sa.Column('trade_ts', sa.DateTime(timezone=True), nullable=False),
    sa.Column('provider', sa.Text(), nullable=False),
    sa.Column('session_date', sa.Date(), nullable=False),
    sa.Column('trade_seq', sa.Integer(), nullable=False),
    sa.Column('symbol', sa.Text(), nullable=True),
    sa.Column('token', sa.Text(), nullable=True),
    sa.Column('price', sa.Float(), nullable=False),
    sa.Column('qty', sa.BigInteger(), nullable=False),
    sa.Column('side', sa.Text(), nullable=False),
    sa.Column('method', sa.Text(), nullable=True),
    sa.Column('classifier_name', sa.Text(), nullable=False),
    sa.Column('classifier_version', sa.Text(), nullable=False),
    sa.Column('best_bid', sa.Float(), nullable=True),
    sa.Column('best_ask', sa.Float(), nullable=True),
    sa.Column('cumulative_volume', sa.BigInteger(), nullable=True),
    sa.Column('features', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('recorded_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("side IN ('BUY', 'SELL')", name=op.f('ck_trades_side_valid')),
    sa.CheckConstraint('qty > 0', name=op.f('ck_trades_qty_positive')),
    sa.UniqueConstraint('provider', 'session_date', 'trade_seq', 'trade_ts', name=op.f('uq_trades_provider_session_date_trade_seq_trade_ts'))
    )
    op.create_index('ix_trades_symbol_trade_ts', 'trades', ['symbol', sa.literal_column('trade_ts DESC')], unique=False)

    # No default time index: the unique keys (instrument + time) already serve the
    # query patterns, and an extra index on every tick insert only costs writes.
    for table, time_column in (("raw_ticks", "received_at"), ("trades", "trade_ts")):
        op.execute(f"SELECT create_hypertable('{table}', by_range('{time_column}', INTERVAL '1 day'), "
                   f"create_default_indexes => false)")

    versions = sa.table("algorithm_versions", sa.column("kind", sa.Text), sa.column("name", sa.Text),
                        sa.column("version", sa.Text), sa.column("description", sa.Text))
    op.bulk_insert(versions, [
        {"kind": "classifier", "name": "vtrender_reconstruction", "version": "v1",
         "description": "Midpoint rule (P < mid BUY, else SELL) with stale-quote tick-rule fallback; 54/55 on the verified set"},
        {"kind": "classifier", "name": "lee_ready", "version": "v1", "description": "Textbook Lee-Ready (reference, 13/55)"},
        {"kind": "classifier", "name": "tick_rule", "version": "v1", "description": "Tick rule only (reference)"},
        {"kind": "engine", "name": "volume_extraction", "version": "v1", "description": "Cumulative-volume diff"},
        {"kind": "engine", "name": "footprint", "version": "v1", "description": "60 s native candles, tick-size price levels"},
        {"kind": "engine", "name": "cvd", "version": "v1", "description": "Sum of closed-candle deltas"},
        {"kind": "engine", "name": "value_area", "version": "v1", "description": "POC-outward expansion, ties go down"},
        {"kind": "engine", "name": "stacked_imbalance", "version": "v1", "description": "Engine rule, unvalidated against Vtrender"},
    ])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_trades_symbol_trade_ts', table_name='trades')
    op.drop_table('trades')
    op.drop_table('raw_ticks')
    op.drop_index(op.f('ix_instruments_symbol'), table_name='instruments')
    op.drop_index(op.f('ix_instruments_name_instrument_type_expiry'), table_name='instruments')
    op.drop_table('instruments')
    op.drop_index('ix_data_quality_events_kind_detected_at', table_name='data_quality_events')
    op.drop_index('ix_data_quality_events_detected_at', table_name='data_quality_events')
    op.drop_table('data_quality_events')
    op.drop_table('algorithm_versions')

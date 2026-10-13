"""trades.open_interest, backfilled from raw_ticks

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-13

Each trade now stores the open interest of the tick that produced it, so the
footprint's OI / dOI rows come from the same rows as volume and sides. Trades
recorded before this column existed are filled from the stored raw ticks of the
same token: the latest tick at or before the trade's exchange time, received
within two minutes of it. Trades without a token (recorded before contract
tagging) or without a matching tick stay NULL (shown as unknown, never guessed).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0005'
down_revision: Union[str, Sequence[str], None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BACKFILL = """
UPDATE trades t SET open_interest = (
    SELECT r.open_interest FROM raw_ticks r
    WHERE r.provider = t.provider AND r.token = t.token
      AND r.received_at BETWEEN t.trade_ts - interval '2 minutes' AND t.trade_ts + interval '2 minutes'
      AND r.last_trade_ts <= t.trade_ts AND r.open_interest IS NOT NULL
    ORDER BY r.last_trade_ts DESC, r.received_at DESC
    LIMIT 1)
WHERE t.open_interest IS NULL AND t.token IS NOT NULL
"""


def upgrade() -> None:
    op.add_column('trades', sa.Column('open_interest', sa.BigInteger(), nullable=True))
    op.execute(BACKFILL)


def downgrade() -> None:
    op.drop_column('trades', 'open_interest')

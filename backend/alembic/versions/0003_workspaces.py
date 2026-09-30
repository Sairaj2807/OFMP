"""workspaces

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-30 21:46:25.916120

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('workspaces',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('owner_user_id', sa.UUID(), nullable=False),
    sa.Column('organization_id', sa.UUID(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('config_version', sa.Integer(), nullable=False),
    sa.Column('revision', sa.Integer(), server_default='1', nullable=False),
    sa.Column('is_default', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint('length(name) BETWEEN 1 AND 80', name=op.f('ck_workspaces_name_length')),
    sa.CheckConstraint('revision >= 1', name=op.f('ck_workspaces_revision_positive')),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], name=op.f('fk_workspaces_organization_id_organizations'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['owner_user_id'], ['users.id'], name=op.f('fk_workspaces_owner_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_workspaces'))
    )
    op.create_index('ix_workspaces_owner_active', 'workspaces', ['owner_user_id'], unique=False, postgresql_where=sa.text('deleted_at IS NULL'))
    op.create_index('uq_workspaces_owner_name_active', 'workspaces', ['owner_user_id', 'name'], unique=True, postgresql_where=sa.text('deleted_at IS NULL'))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('uq_workspaces_owner_name_active', table_name='workspaces', postgresql_where=sa.text('deleted_at IS NULL'))
    op.drop_index('ix_workspaces_owner_active', table_name='workspaces', postgresql_where=sa.text('deleted_at IS NULL'))
    op.drop_table('workspaces')

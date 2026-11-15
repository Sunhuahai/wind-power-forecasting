"""NWP revisions keyed by issue time, for real-time ingestion with late and revised batches."""
from alembic import op
from sqlalchemy import Column, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Table
from windpower.db.schema import metadata

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None

nwp_revisions = Table(
    'nwp_revisions', metadata,
    Column('farm_id', ForeignKey('wind_farms.id'), primary_key=True),
    Column('issued_at', DateTime, primary_key=True),
    Column('valid_at', DateTime, primary_key=True),
    Column('received_at', DateTime, nullable=False),
    Column('source', String, nullable=False),
    Column('u10', Float), Column('v10', Float), Column('u100', Float), Column('v100', Float),
    Column('quality', JSON, nullable=False),
)


def upgrade():
    nwp_revisions.create(op.get_bind(), checkfirst=True)
    Index('ix_nwp_revisions_valid', nwp_revisions.c.farm_id, nwp_revisions.c.valid_at).create(
        op.get_bind(), checkfirst=True)


def downgrade():
    Index('ix_nwp_revisions_valid', nwp_revisions.c.farm_id, nwp_revisions.c.valid_at).drop(
        op.get_bind(), checkfirst=True)
    nwp_revisions.drop(op.get_bind(), checkfirst=True)

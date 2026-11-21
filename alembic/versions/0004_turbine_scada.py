"""单机 SCADA 增量迁移：文件登记、风机资产、原始与规范 10 分钟数据。

不修改 0001—0003 的历史语义；GEFCom2014 实验表与场站 operational_* 表不受影响。
新表先作为标准 PostgreSQL 表；TimescaleDB hypertable 激活见
docs/部署与工程/turbine-timescale-activation.sql，须独立预检后执行。
源时间为无时区字符串：turbine_scada_10m.observed_at 保持 naive，
time_semantics 记录在 data_ingest_files，未经源系统确认不得转 UTC。
"""
from alembic import op
from windpower.db.turbine_schema import turbine_metadata

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade():
    turbine_metadata.create_all(op.get_bind())
    # 跨 schema 模块的外键：wind_farms 属于既有实验 metadata，这里显式补建。
    op.create_foreign_key('fk_wind_turbines_farm', 'wind_turbines',
                          'wind_farms', ['farm_id'], ['id'])


def downgrade():
    bind = op.get_bind()
    has_ts = bind.execute(
        __import__('sqlalchemy').text(
            "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname='timescaledb')"
        )).scalar()
    if has_ts:
        from sqlalchemy import text as _text
        active = bind.execute(_text(
            "SELECT hypertable_name FROM timescaledb_information.hypertables "
            "WHERE hypertable_name IN ('turbine_scada_10m','turbine_power_15m_proxy')"
        )).scalars().all()
        if active:
            raise RuntimeError(
                f"Active Timescale hypertables {active}: restore an audited backup instead")
    op.drop_constraint('fk_wind_turbines_farm', 'wind_turbines', type_='foreignkey')
    for table in ('feature_snapshot_manifest', 'turbine_power_15m_proxy',
                  'turbine_scada_10m', 'turbine_scada_raw_10m',
                  'wind_turbines', 'data_ingest_files'):
        op.drop_table(table)

"""单台风机 SCADA 存储层：与场站 operational_* 表分层，与 GEFCom2014 实验表隔离。

设计约束（docs/周进展/2026-W47（11-16～11-22）/02 规划 §4）：
- 单机网侧功率允许负值（kW），不得写入场站 operational_power（MW、非负）。
- 源时间为无时区字符串，语义未经确认：规范时间列保持 naive，原字符串另存，
  只有源系统确认时区与区间起止后才允许转 UTC。
- 高频表主键包含时间列，预留 TimescaleDB hypertable；激活脚本独立执行。
- 修订采用追加式版本（file_id 区分），重复导入同一文件不产生重复记录。
"""
from sqlalchemy import (
    Boolean, CheckConstraint, Column, DateTime, Float, ForeignKey,
    Index, Integer, MetaData, String, Table,
)
from sqlalchemy.dialects.postgresql import JSONB

turbine_metadata = MetaData()

data_ingest_files = Table(
    'data_ingest_files', turbine_metadata,
    Column('file_id', String(64), primary_key=True),  # sha256，天然幂等键
    Column('file_name', String(256), nullable=False),
    Column('sha256', String(64), nullable=False),
    Column('byte_size', Integer, nullable=False),
    Column('encoding', String(32), nullable=False),
    Column('row_count', Integer, nullable=False),
    Column('column_count', Integer, nullable=False),
    Column('time_min', DateTime(), nullable=False),
    Column('time_max', DateTime(), nullable=False),
    Column('time_semantics', String(128), nullable=False),
    Column('ingested_at', DateTime(timezone=True), nullable=False),
    Column('ingester_version', String(64), nullable=False),
    Column('profile', JSONB, nullable=False),
    Column('raw_path', String(1024), nullable=False),
    CheckConstraint('byte_size > 0', name='ck_file_size'),
    CheckConstraint('row_count > 0', name='ck_file_rows'),
)

wind_turbines = Table(
    'wind_turbines', turbine_metadata,
    Column('turbine_id', Integer, primary_key=True),
    # farm_id 指向既有 wind_farms（另一个 metadata）；外键约束由 0004 迁移显式创建。
    Column('farm_id', Integer, nullable=True),
    Column('external_id', String(64), nullable=False),
    Column('model_name', String(128), nullable=False),
    Column('rated_power_kw', Float, nullable=True),
    Column('rated_power_evidence', String(512), nullable=True),
    Column('feeder_name', String(128), nullable=True),
    Column('recorded_at', DateTime(timezone=True), nullable=False),
    Column('notes', JSONB, nullable=False),
    Column('source_file_id', String(64), ForeignKey('data_ingest_files.file_id'), nullable=False),
    CheckConstraint("farm_id IS NULL OR farm_id >= 100", name='ck_turbine_farm_namespace'),
    # GEFCom2014 场站 id 为 1..10；真实场站另用 >=100 的命名空间，防止误挂到实验 Zone。
)

turbine_scada_raw_10m = Table(
    'turbine_scada_raw_10m', turbine_metadata,
    Column('file_id', String(64), ForeignKey('data_ingest_files.file_id'), primary_key=True),
    Column('row_no', Integer, primary_key=True),  # 数据行号，1 起，不含表头
    Column('raw_time_text', String(32), nullable=False),
    Column('payload', JSONB, nullable=False),  # 全部原始字符串字段，含 '--' 与哨兵值
    Column('ingested_at', DateTime(timezone=True), nullable=False),
)

turbine_scada_10m = Table(
    'turbine_scada_10m', turbine_metadata,
    Column('turbine_id', Integer, ForeignKey('wind_turbines.turbine_id'), primary_key=True),
    Column('observed_at', DateTime(), primary_key=True),  # 源 naive 时钟，未确认时区
    Column('file_id', String(64), ForeignKey('data_ingest_files.file_id'), primary_key=True),
    Column('grid_active_power_kw', Float),  # 候选标签；负值为真实净取用，不得截零
    Column('converter_active_power_kw', Float),
    Column('converter_reactive_kvar', Float),
    Column('grid_reactive_kvar', Float),
    Column('nacelle_wind_speed_ms', Float),
    Column('ambient_temp_c', Float),
    Column('rotor_rpm', Float),
    Column('generator_rpm', Float),
    Column('pitch1_deg', Float),
    Column('pitch2_deg', Float),
    Column('pitch3_deg', Float),
    Column('yaw_position_deg', Float),  # 原始累计角度，不做取模
    Column('yaw_error_deg', Float),
    Column('generating_h', Float),
    Column('fault_h', Float),
    Column('maintenance_h', Float),
    Column('normal_h', Float),
    Column('env_abnormal_h', Float),
    Column('env_normal_h', Float),
    Column('quality_flags', JSONB, nullable=False),
    Column('raw_ref_row_no', Integer, nullable=False),
    Column('raw_ref_file_id', String(64), nullable=False),
    CheckConstraint('turbine_id >= 1', name='ck_scada_turbine'),
)
Index('ix_turbine_scada_10m_time', turbine_scada_10m.c.turbine_id,
      turbine_scada_10m.c.observed_at, turbine_scada_10m.c.file_id)

turbine_power_15m_proxy = Table(
    'turbine_power_15m_proxy', turbine_metadata,
    Column('turbine_id', Integer, ForeignKey('wind_turbines.turbine_id'), primary_key=True),
    Column('valid_at', DateTime(), primary_key=True),  # 15 分钟时段起点（假设，未确认）
    Column('file_id', String(64), ForeignKey('data_ingest_files.file_id'), primary_key=True),
    Column('method', String(64), nullable=False),
    Column('proxy_kw', Float, nullable=False),
    Column('covered_minutes', Integer, nullable=False),
    Column('contributing_rows', JSONB, nullable=False),  # 原始 10 分钟行引用与重叠分钟权重
    Column('research_only', Boolean, nullable=False),
    Column('label_semantics_confirmed', Boolean, nullable=False),
    CheckConstraint('covered_minutes = 15', name='ck_proxy_full_coverage'),
    CheckConstraint('research_only', name='ck_proxy_research_only'),
    # 语义未确认前本表不得写入真实数据（规划 §6）；转换函数与合成测试见 data/resample_15m.py。
)

feature_snapshot_manifest = Table(
    'feature_snapshot_manifest', turbine_metadata,
    Column('snapshot_id', String(64), primary_key=True),
    Column('task', String(64), nullable=False),
    Column('dataset_file_id', String(64), ForeignKey('data_ingest_files.file_id'), nullable=False),
    Column('turbine_id', Integer, ForeignKey('wind_turbines.turbine_id'), nullable=False),
    Column('created_at', DateTime(timezone=True), nullable=False),
    Column('availability_assumption', String(256), nullable=False),
    Column('config', JSONB, nullable=False),
    Column('feature_columns', JSONB, nullable=False),
    CheckConstraint("task IN ('F01_NATIVE_10M','F01_PROXY_15M')", name='ck_snapshot_task'),
)

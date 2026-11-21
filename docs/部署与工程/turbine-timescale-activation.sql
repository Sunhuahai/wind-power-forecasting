-- 单机 SCADA TimescaleDB 激活；只在部署环境预检、备份后执行。
-- 前提：Alembic 已升级至 0004；PostgreSQL 18.6 + 兼容的 TimescaleDB 2.32.x；
-- 插件由管理员安装/预加载。绝不对旧 GEFCom2014 小时级实验表做转换。
-- turbine_scada_10m 在 F01 导入后含真实数据：migrate_data=>true 需要停写窗口与已验证备份。
-- turbine_power_15m_proxy 预计为空（源区间语义未确认前不写入真实代理标签）。
\set ON_ERROR_STOP on

SELECT version();
SHOW shared_preload_libraries;
SELECT extname, extversion FROM pg_extension WHERE extname='timescaledb';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname='timescaledb') THEN
        RAISE EXCEPTION 'TimescaleDB extension not installed; abort';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname='timescaledb'
                    AND extversion LIKE '2.3%') THEN
        RAISE NOTICE 'Unverified TimescaleDB major version; check compatibility matrix';
    END IF;
END $$;

-- 主键均含时间列（observed_at / valid_at），满足 hypertable 唯一约束要求。
-- 外键方向为 hypertable -> 普通表（wind_turbines / data_ingest_files），TimescaleDB 支持。
-- migrate_data 涉及既有 F01 行：执行前必须完成备份并停止导入任务。
SELECT create_hypertable('turbine_scada_10m', by_range('observed_at'),
                         chunk_time_interval => INTERVAL '30 days',
                         migrate_data => true, if_not_exists => true);
SELECT create_hypertable('turbine_power_15m_proxy', by_range('valid_at'),
                         chunk_time_interval => INTERVAL '90 days',
                         if_not_exists => true);

SELECT hypertable_name, num_chunks
  FROM timescaledb_information.hypertables
 WHERE hypertable_name IN ('turbine_scada_10m','turbine_power_15m_proxy')
 ORDER BY 1;

-- 文件登记、风机资产、原始行存、快照清单保持普通 PG 表。
-- observed_at/valid_at 为源 naive 时钟：hypertable 分区按源时间顺序推进，
-- 转为 TIMESTAMPTZ 必须等源系统确认时区后另行编制变更单。
-- 不设置自动 retention：原始与审计证据按场站合规要求留存。

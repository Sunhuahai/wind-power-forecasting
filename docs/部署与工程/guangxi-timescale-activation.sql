-- 广西真实场站专用 TimescaleDB 激活；只在部署环境预检、备份后执行。
-- 前提：Alembic 已升级至 0003；PostgreSQL 18.6 + 兼容的 TimescaleDB 2.32.x；
-- 插件由管理员安装/预加载。绝不对旧 GEFCom2014 小时级实验表做转换。
-- 生产前验证官方兼容性矩阵与许可。脚本不启用自动数据删除或压缩。
-- 涉及真实数据的转换应另行编制变更单，并以可恢复备份为前提。
-- 2026-11-21 实测修订：create_hypertable 使用经典 (relation, time_column) 重载；
-- 维度式 by_range(...) 与 chunk_time_interval 关键字不能混用。
\set ON_ERROR_STOP on

SELECT version();
SHOW shared_preload_libraries;
SELECT extname, extversion FROM pg_extension WHERE extname='timescaledb';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname='timescaledb') THEN
        RAISE EXCEPTION 'TimescaleDB extension not installed; abort';
    END IF;
    IF EXISTS (SELECT 1 FROM operational_power LIMIT 1)
       OR EXISTS (SELECT 1 FROM operational_nwp LIMIT 1)
       OR EXISTS (SELECT 1 FROM forecast_values LIMIT 1) THEN
        RAISE EXCEPTION 'Data already exist: require audited migrate_data plan; abort';
    END IF;
END $$;

-- 三个高频表的主键均含 valid_at，满足 hypertable 唯一约束要求。
-- 所有外键均指向普通关系表，不构造 hypertable -> hypertable 外键。
SELECT create_hypertable('operational_power', 'valid_at',
                         chunk_time_interval => INTERVAL '30 days',
                         if_not_exists => true);
SELECT create_hypertable('operational_nwp', 'valid_at',
                         chunk_time_interval => INTERVAL '7 days',
                         if_not_exists => true);
SELECT create_hypertable('forecast_values', 'valid_at',
                         chunk_time_interval => INTERVAL '7 days',
                         if_not_exists => true);

SELECT hypertable_name, num_chunks
  FROM timescaledb_information.hypertables
 WHERE hypertable_name IN ('operational_power','operational_nwp','forecast_values')
 ORDER BY 1;

-- 站点/容量/运行事件/预测发布/报送/回执/政策版本/评价结果保持普通 PG 表。
-- 根据实际写入速率、修订延迟、审计留存期限和内存再选择列存压缩策略。
-- 不设置自动 retention：报送与政策考核证据必须满足留存要求。

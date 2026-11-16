-- TimescaleDB 迁移草案（未执行）
-- =====================================================================
-- 状态：草案。用户 2026-11-16 决定"先不跑真机验证，等真实数据"，
--       因此本文件不注册为 Alembic 版本、不参与 `alembic upgrade head`，
--       避免在不受支持的实例上误执行。
--
-- 目标环境：PostgreSQL 18.6 + TimescaleDB 2.32.x（Linux）
-- 不可用于：本机开发库（PostgreSQL 18.7 不在 2.32.0 官方支持范围）
--
-- 跑之前必须先读：docs/timescaledb-evaluation.md 第 7 节
-- 回滚方式：TimescaleDB 没有"撤销 hypertable"的官方路径，
--           回滚只能靠迁移前的 `pg_dump -Fc` 全量备份。
-- =====================================================================

-- ---------------------------------------------------------------------
-- 0. 前置检查（只读，先跑这四条，任一条不满足就停下来）
-- ---------------------------------------------------------------------
-- 0.1 版本是否匹配（应为 18.6，扩展应为 2.32.x）
SELECT version();
SELECT extversion FROM pg_extension WHERE extname = 'timescaledb';

-- 0.2 扩展是否已预加载（shared_preload_libraries 必须含 timescaledb）
SHOW shared_preload_libraries;

-- 0.3 四张目标表的主键是否都包含时间列（必须全部为 t）
--     唯一索引必须包含全部分区列，否则 create_hypertable 会失败。
SELECT c.relname AS table_name,
       con.conname,
       pg_get_constraintdef(con.oid) AS definition
  FROM pg_constraint con
  JOIN pg_class c ON c.oid = con.conrelid
 WHERE con.contype = 'p'
   AND c.relname IN ('power_observations','nwp_forecasts','nwp_revisions','predictions')
 ORDER BY 1;

-- 0.4 时间列是否全部非空（分区列不允许 NULL）
SELECT 'power_observations' AS t, count(*) FILTER (WHERE observed_at IS NULL) AS nulls FROM power_observations
UNION ALL SELECT 'nwp_forecasts',  count(*) FILTER (WHERE valid_at IS NULL) FROM nwp_forecasts
UNION ALL SELECT 'nwp_revisions',  count(*) FILTER (WHERE valid_at IS NULL) FROM nwp_revisions
UNION ALL SELECT 'predictions',    count(*) FILTER (WHERE valid_at IS NULL) FROM predictions;

-- ---------------------------------------------------------------------
-- 1. 安装扩展（需要超级用户；应用账号不要有这个权限）
-- ---------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ---------------------------------------------------------------------
-- 2. 大表转 hypertable
--    只按时间分区，不加空间（farm_id 哈希）维度：
--    10–100 个站的站内查询靠普通索引 + 分区裁剪即可，
--    加空间维度会让 chunk 数乘以分区数，得不偿失。
--    chunk 间隔是起步值：上线后按"活跃 chunk 索引 ≤ 内存 25%"复核再调。
-- ---------------------------------------------------------------------
SELECT create_hypertable('power_observations', by_range('observed_at'),
                         chunk_time_interval => INTERVAL '30 days',
                         migrate_data => true);

SELECT create_hypertable('nwp_forecasts', by_range('valid_at'),
                         chunk_time_interval => INTERVAL '7 days',
                         migrate_data => true);

SELECT create_hypertable('nwp_revisions', by_range('valid_at'),
                         chunk_time_interval => INTERVAL '7 days',
                         migrate_data => true);

SELECT create_hypertable('predictions', by_range('valid_at'),
                         chunk_time_interval => INTERVAL '7 days',
                         migrate_data => true);

-- wind_farms / model_runs / dataset_splits 保持普通表：
-- hypertable 之间不支持外键，必须留一侧为普通表。

-- ---------------------------------------------------------------------
-- 3. 冷数据列存（compression）
--    阈值必须大于最大修订延迟，否则迟到修订会落进已压缩 chunk，
--    触发"先转回行存再改"的高成本路径。
--    起步用 90 天；稳态后用 nwp_revisions 的 received_at-valid_at 分布复核。
-- ---------------------------------------------------------------------
ALTER TABLE nwp_revisions SET (timescaledb.compress = true);
SELECT add_columnstore_policy('nwp_revisions', INTERVAL '90 days');

ALTER TABLE nwp_forecasts SET (timescaledb.compress = true);
SELECT add_columnstore_policy('nwp_forecasts', INTERVAL '180 days');

ALTER TABLE predictions SET (timescaledb.compress = true);
SELECT add_columnstore_policy('predictions', INTERVAL '180 days');

-- 功率表数据量小，暂不压缩；等留存量上来再开：
-- ALTER TABLE power_observations SET (timescaledb.compress = true);
-- SELECT add_columnstore_policy('power_observations', INTERVAL '365 days');

-- ---------------------------------------------------------------------
-- 4. 保留策略（先注释掉，等确认合规与留存年限再开）
--    DROP CHUNK 是瞬时元数据操作，不可撤销。
-- ---------------------------------------------------------------------
-- SELECT add_retention_policy('nwp_forecasts',  INTERVAL '2 years');
-- SELECT add_retention_policy('nwp_revisions',  INTERVAL '2 years');
-- SELECT add_retention_policy('predictions',    INTERVAL '3 years');
-- SELECT add_retention_policy('power_observations', INTERVAL '5 years');

-- ---------------------------------------------------------------------
-- 5. 连续聚合（报表用；先建两张，看板接上后再加）
--    注意：连续聚合的查询有限制（不能随意窗口函数/自连接），
--    口径变更要重建视图。
-- ---------------------------------------------------------------------
-- 5.1 分站 × 提前量 的误差口径（按天聚合，长期看板用）
-- CREATE MATERIALIZED VIEW IF NOT EXISTS cagg_error_daily
-- WITH (timescaledb.continuous) AS
-- SELECT time_bucket(INTERVAL '1 day', valid_at) AS bucket,
--        run_id,
--        lead,
--        count(*)                                        AS n,
--        avg(prediction - actual)                         AS bias,
--        avg((prediction - actual) * (prediction - actual)) AS mse
--   FROM predictions
--  WHERE actual IS NOT NULL
--  GROUP BY 1, 2, 3
-- WITH NO DATA;
--
-- SELECT add_continuous_aggregate_policy('cagg_error_daily',
--        start_offset => INTERVAL '30 days',
--        end_offset   => INTERVAL '1 day',
--        schedule_interval => INTERVAL '1 hour');

-- 5.2 日可用率（观测缺测/质量标记）
-- CREATE MATERIALIZED VIEW IF NOT EXISTS cagg_availability_daily
-- WITH (timescaledb.continuous) AS
-- SELECT time_bucket(INTERVAL '1 day', observed_at) AS bucket,
--        farm_id,
--        count(*)                                        AS expected_rows,
--        count(power)                                    AS present_rows
--   FROM power_observations
--  GROUP BY 1, 2
-- WITH NO DATA;

-- ---------------------------------------------------------------------
-- 6. 迁移后校验（与源库逐表比对，四项都要一致）
-- ---------------------------------------------------------------------
SELECT 'power_observations' AS t, count(*), round(sum(power)::numeric, 6),
       min(observed_at), max(observed_at) FROM power_observations
UNION ALL
SELECT 'nwp_forecasts', count(*), round(sum(u100)::numeric, 6),
       min(valid_at), max(valid_at) FROM nwp_forecasts
UNION ALL
SELECT 'nwp_revisions', count(*), round(sum(u100)::numeric, 6),
       min(valid_at), max(valid_at) FROM nwp_revisions
UNION ALL
SELECT 'predictions', count(*), round(sum(prediction)::numeric, 6),
       min(valid_at), max(valid_at) FROM predictions;

-- 结构确认
SELECT hypertable_name, num_chunks
  FROM timescaledb_information.hypertables
 ORDER BY 1;

SELECT hypertable_name, pg_size_pretty(before_compression_total_bytes) AS before,
       pg_size_pretty(after_compression_total_bytes) AS after
  FROM hypertable_columnstore_stats;

-- ---------------------------------------------------------------------
-- 7. 运维口径（写进手册，不在这里执行）
-- ---------------------------------------------------------------------
-- 升级顺序：先升 TimescaleDB，再升 PostgreSQL（官方要求）。
-- 备份：pgBackRest 全量 + WAL 归档；恢复前跑 timescaledb_pre_restore()，
--       恢复后跑 timescaledb_post_restore()。
-- HA：PG 原生流复制 + Patroni/repmgr；不要用逻辑复制同步 TimescaleDB。
-- 作业监控：timescaledb_information.job_stats 里 last_run_status 非 Success 即告警。
-- 应用侧注意：upsert 不得修改时间列（跨 chunk 更新不支持）；
--             现有 windpower.db.core.upsert() 主键含时间列，安全。

-- ---------------------------------------------------------------------
-- 8. 生产新表规范（真实数据接入时按这套建，不要沿用 2012 实验表的类型）
-- ---------------------------------------------------------------------
-- 8.1 时间列一律 TIMESTAMPTZ（官方对时间维度列的首选），
--     不要在应用层心算时区；展示层再做本地化。
-- 8.2 高频 upsert 的行存 chunk：fillfactor 90 + 激进 autovacuum。
--     ALTER TABLE predictions SET (
--         fillfactor = 90,
--         autovacuum_vacuum_scale_factor = 0.02,
--         autovacuum_analyze_scale_factor = 0.01
--     );
-- 8.3 入库时间（received_at / arrival_time）建 BRIN：
--     CREATE INDEX IF NOT EXISTS ix_nwp_revisions_received_brin
--         ON nwp_revisions USING BRIN (received_at);
-- 8.4 风向不要存 0~360 的角度：存 u/v 分量（现有 2012 表已是 u100/v100），
--     角度在 0/360 处不连续，模型与统计都会被带偏。
-- 8.5 空气密度这类派生量用 GENERATED ALWAYS AS 生成列，
--     不要独立落库，避免与温度/气压不一致。
-- 8.6 概率预测（p10/p90）与点预测同表落库，方便按区间评分；
--     模型权重存文件系统 + sha256，库里只存路径（现有 bundle manifest 已是此形态）。

# 数据库选型调研（2026-11-16）

> 结论先行：**继续用 PostgreSQL 作为正式主库**，结构化数据与实验记录都放进去，原始文件和模型权重放文件系统；TimescaleDB、DuckDB、ClickHouse 作为"出现明确负载后再引入"的扩展。
> 依据：官方文档（2026-11-16 查阅）+ 本项目仓库代码与实测数据。功能比较来自官方资料，"是否适合我们"是工程判断，不是跑分结论。

## 一、我们的需求画像

| 维度 | 项目现状（实测） | 对选型的影响 |
|---|---|---|
| 数据规模 | 功率观测 175,440 行、NWP 175,440 行、预测 127,296 行、实验 30 条，整库 **115 MB** | 当前规模很小，不需要专用时序系统 |
| 数据关系 | 功率属场站、预测属实验、实验关联模型与配置 | 需要关系模型与外键 |
| 写入模式 | 批量导入 + 每日预测回写，重复导入要幂等 | 需要唯一键 + 事务 |
| 查询模式 | 按时间窗取历史、按场站/提前量聚合误差 | 需要时间索引与分组聚合 |
| 历史回放 | 要能回答"当时那一刻，哪些预报真的可用" | 需要发布/到达/有效三种时间 |
| 实验追溯 | 每条预测要能追到模型、参数、数据版本 | 需要实验表与输入版本标识 |
| 实时预测（在准备） | 每日出一次 24 小时预报，10 站 | 需要并发连接、备份、可恢复 |

一句话：**我们要的是"能管好数据关系、能保证重复写入可控、能追溯实验"的通用关系库**，而不是单纯的时序存储。

## 二、候选清单：定位、关键机制、适配度

适配度按 8 个维度加权（权重见第三节），满分 100，是工程判断。

| 数据库 | 定位 | 关键机制/参数 | 主要代价 | 适配度 | 结论 |
|---|---|---|---|---|---|
| **PostgreSQL** | 通用关系库 | MVCC、WAL、主外键与 CHECK、JSONB、原生分区、`ON CONFLICT`、流复制、扩展生态 | 要自己运维服务、索引与备份 | **89** | **主库** |
| MySQL / InnoDB | 通用关系库 | 事务、外键、行级锁、JSON、窗口函数；分区与外键有兼容限制 | 迁移需重写数据访问与验收 | 79 | 合格备选，无迁移理由 |
| SQLite | 嵌入式关系库 | 单文件、WAL 模式；同一时刻一个写者 | 多进程共享服务受限 | 65 | 适合本地工具，不适合服务 |
| TimescaleDB | PostgreSQL 时序扩展 | hypertable、`chunk_time_interval`、压缩、连续聚合；唯一索引须含时间列，hypertable 间外键有限制 | 多维护一套扩展版本与策略 | 84 | **首选时序增强路线**，当前不上 |
| DuckDB | 嵌入式列式分析 | 进程内 MVCC + 乐观并发（写冲突报错）；多进程需 Quack 或 DuckLake（PostgreSQL 做 catalog）；Parquet 原生 | 不是共享服务型主库 | 55 | 离线分析工具 |
| ClickHouse | 列式分析库 | MergeTree、副本表、向量化扫描；多语句事务仍标实验性 | 事务型业务不匹配 | 53 | 将来做分析副库 |
| InfluxDB 3 | 设备遥测时序库 | SQL + JOIN；Core/Enterprise/Cloud 能力不同 | 通用关系管理不是强项 | 51 | 独立监控平台再评估 |
| MongoDB | 文档库 | 文档模型；时序集合有唯一索引/事务限制 | 我们已有明确关系 | 53 | 无必要 |
| Redis | 内存缓存 | RDB/AOF 持久化、单线程执行命令 | 不适合长期关系数据 | 42 | 接口压力大时做缓存 |
| Parquet | 列式**文件格式**（非数据库） | 列存、压缩、可直接被 DuckDB 查询 | 无事务、无约束、无服务 | 32 | 检查导出与数据快照 |

### 各候选补充说明

- **PostgreSQL**：`ON CONFLICT DO UPDATE` 提供原子幂等写入；JSONB 可索引，适合存模型参数与质量标记；原生分区能按时间切大表；流复制 + WAL 归档能做时间点恢复。本项目 6 张核心表已全部围绕它实现。
- **MySQL**：能力上完全能承担本项目，差别主要在"迁移成本"和"后续时序扩展的连贯性"。若团队统一用 MySQL 且有现成运维，也合理。
- **SQLite**：单写者限制决定了它不适合"导入、训练、预测回写同时跑"的服务形态。
- **TimescaleDB**：对我们真正有价值的是自动时间分块、连续聚合与压缩；但这些收益要等表大到"分区维护成为负担"才体现。引入时只能挑大表（功率、NWP 修订、预测）转 hypertable，实验与配置表保持普通表。
- **DuckDB**：适合"把导出的 Parquet 拿来反复做 SQL 分析"，不适合当共享主库——它的并发模型是单进程读写，多进程要么走 Quack，要么用 DuckLake + PostgreSQL catalog。
- **ClickHouse**：适合海量误差明细的扫描聚合；但把它当业务主库会丢掉我们依赖的事务与约束。
- **InfluxDB 3**：设备遥测场景强，但我们更需要"场站—实验—预测"的关系管理。
- **MongoDB / Redis / Parquet**：分别缺关系约束、缺长期存储定位、缺数据库服务能力。

## 三、加权评分明细

| 维度 | 权重 | PG | MySQL | SQLite | Timescale | DuckDB | ClickHouse | Influx3 | Mongo | Redis |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 关系与完整性 | 20 | 5 | 5 | 3 | 5 | 2 | 1 | 1 | 2 | 1 |
| 幂等写入/事务 | 15 | 5 | 5 | 4 | 5 | 3 | 2 | 2 | 3 | 2 |
| 时间范围查询 | 15 | 4 | 4 | 3 | 5 | 4 | 5 | 5 | 4 | 2 |
| 实验追溯 | 10 | 5 | 4 | 3 | 4 | 2 | 2 | 1 | 2 | 1 |
| 实时写入 | 10 | 4 | 4 | 2 | 4 | 1 | 4 | 4 | 3 | 5 |
| 分析聚合 | 10 | 3 | 3 | 2 | 4 | 5 | 5 | 4 | 3 | 1 |
| 运维复杂度（越高越简单） | 10 | 4 | 3 | 5 | 2 | 4 | 2 | 3 | 3 | 4 |
| 迁移成本（越高越省事） | 10 | 5 | 2 | 4 | 3 | 1 | 1 | 1 | 1 | 2 |
| **加权总分** | 100 | **89** | 79 | 65 | 84 | 55 | 53 | 51 | 53 | 42 |

> 分数用于比较"适配我们"，不是性能排名；换一个团队、换一套运维体系，结论可能不同。

## 四、为什么是 PostgreSQL

1. **关系已经写在需求里**：功率属场站、预测属实验、实验属模型；用外键和主键表达最直接。
2. **幂等写入是刚需**：重复导入、任务重试不能产生重复记录，`ON CONFLICT` 一条语句解决。
3. **约束能下沉到数据库**：预测值 0–1、提前量 1–24 已有 CHECK，避免脏数据进库。
4. **固定字段 + 灵活配置共存**：时间、风矢量用强类型；模型参数、质量标记用 JSONB。
5. **回放与版本管理能长在同一个库里**：`nwp_revisions` 已建表，主键含发布时刻，天然支持多批次与修订。
6. **迁移成本最低**：取数、训练、预测回写、验收脚本都已围绕它实现。

对比 MySQL：两者都能满足基础需求；PostgreSQL 的 JSONB 索引、原生分区、以及"未来可平滑叠加 TimescaleDB"的路线，与我们的演进方向更连贯。

## 五、生产部署方案（PostgreSQL）

### 5.1 版本策略

| 项 | 建议 |
|---|---|
| 主版本 | 跟随 18.x（当前 **18.7**，2026-11-12 发布，修复 52 个安全漏洞） |
| 19 的时机 | PostgreSQL 19 计划 **2026-11-19** GA；**不在首个 GA 就上生产**，等 19.1 修复版再评估 |
| 小版本 | 每季度跟随一次小版本，安全公告（CVE）立即评估 |
| 代码一致性 | 当前 `db/core.py` 的 `health()` 硬编码要求 **18.6**，与运行中的 18.7 不一致，**必须改成 18.x 语义**（校验主版本，允许小版本差异） |
| 升级方式 | 小版本：停服替换二进制即可；大版本：`pg_upgrade` 或 dump/restore，先在同版本副本上演练 |

### 5.2 部署形态（按阶段选）

| 阶段 | 形态 | 说明 |
|---|---|---|
| 开发/实验 | 单机本地（现状） | 够用，数据可重建 |
| 生产最小可用 | **单机 + 每日全量备份 + WAL 归档** | RPO ≤ 5 分钟（WAL），RTO ≈ 30–60 分钟 |
| 生产推荐 | **主库 + 1 台流复制备库**（Patroni 或托管服务自动切换） | RPO ≈ 0（同步复制可设），RTO ≤ 1 分钟 |
| 分析隔离 | 备库承接报表与批量导出 | 避免大查询影响预测写入 |

> 十站、每日一次预报的负载，**单机 + WAL 归档已经足够**；备库是为了"数据库挂了还能出预报"，不是性能需要。

### 5.3 容量规划（估算，非实测上限）

| 场景 | 每年行数 | 存储估算 |
|---|---:|---:|
| 当前：10 站 × 每小时 | 87,600（功率）+ 87,600（NWP） | 整库约 115 MB |
| 100 站 × 15 分钟 | 3,504,000 | 约 0.5–1 GB/年 |
| NWP 多批次：10 站 × 4 批/日 × 72 目标时刻 | 1,051,200 | 约 0.3 GB/年 |
| 预测：10 站 × 1 次/日 × 24 小时 × 3 个模型版本 | 262,800 | 约 0.1 GB/年 |

结论：**三年内单机 SSD 200 GB 绰绰有余**；真正会先涨起来的是"多批次 NWP + 多模型版本预测"，所以这两张表要一开始就设计好分区。

### 5.4 服务器与参数（以 16 GB 内存 / 4 vCPU / SSD 为例）

| 参数 | 建议值 | 理由 |
|---|---|---|
| `shared_buffers` | 4 GB（约 25%） | 常规起点 |
| `effective_cache_size` | 12 GB（约 75%） | 告诉规划器 OS 缓存可用量 |
| `work_mem` | 32 MB | 聚合/排序够用，避免过高导致内存爆 |
| `maintenance_work_mem` | 1 GB | 建索引、VACUUM 更快 |
| `wal_level` | `replica` | 支持复制与归档 |
| `max_wal_size` | 8 GB | 减少检查点抖动 |
| `checkpoint_completion_target` | 0.9 | 平滑写盘 |
| `random_page_cost` | 1.1 | SSD |
| `effective_io_concurrency` | 200 | SSD |
| `timezone` | `UTC` | 与"无时区源时钟"约定分离，避免双重转换 |
| `log_min_duration_statement` | 200 ms | 慢查询留痕 |
| `idle_in_transaction_session_timeout` | 60 s | 防止长事务拖住 VACUUM |
| `statement_timeout`（应用角色） | 30 s | 防止误查拖垮库 |
| `autovacuum` | 开，表级按写入量调 | 时序表更新少、插入多，需单独观察 |

### 5.5 数据库对象设计

- **时间语义分开存**：`observed_at`（观测）、`issued_at`（发布）、`received_at`（到达）、`valid_at`（有效）、`origin`（预测起点）五个概念不要混用。
- **索引**：
  - `power_observations (farm_id, observed_at)` 复合 B-tree（历史窗口查询）；
  - `nwp_revisions (farm_id, valid_at)` + 发布时刻；
  - `predictions (run_id, origin, valid_at)`；
  - 大表加 `BRIN (valid_at)` 辅助范围扫描。
- **分区**：`nwp_revisions`、`predictions` 按月或按年 RANGE 分区；分区键用 `valid_at`，与主键一致（PostgreSQL 要求分区键包含在主键里）。
- **约束**：功率 0–1、提前量 1–24、外键指向场站与实验，保持现状。
- **JSONB**：`model_runs.config`、`quality` 用 JSONB；需要按字段过滤时再加表达式索引。

### 5.6 迁移与发布

- Alembic 单一 head；CI 跑 `alembic upgrade head` 后再跑测试。
- 迁移脚本遵守"先加列后回填、先建索引 CONCURRENTLY、最后加约束"的顺序，保证可在线执行。
- 发布前在备库或快照上演练；迁移失败必须能回退（每个版本保留 downgrade）。

### 5.7 备份与恢复

| 项 | 方案 |
|---|---|
| 每日全量 | `pg_dump -Fc` 或 `pg_basebackup`，存对象存储 |
| 连续归档 | WAL 归档到对象存储，支持时间点恢复（PITR） |
| 恢复目标 | RPO ≤ 5 分钟；RTO ≤ 1 小时 |
| 演练 | **每月一次恢复演练**，记录实际耗时 |
| 校验 | 备份后校验文件大小与 `pg_restore --list`；每季度做一次"恢复到临时实例并跑验收 SQL" |

### 5.8 高可用与故障切换

- 主库 + 流复制备库（异步或同步视 RPO 要求）。
- 自动切换用 Patroni（含 etcd/Consul）或云托管；**切换后必须防止脑裂**（fencing）。
- 应用侧：连接串指向读写地址，重试逻辑带退避；备库只接只读查询。

### 5.9 监控与告警

| 指标 | 来源 | 告警阈值（建议） |
|---|---|---|
| 慢查询 | `pg_stat_statements` + 慢日志 | 单条 > 2 s 或平均 > 200 ms |
| 连接数 | `pg_stat_activity` | > 80% `max_connections` |
| 复制延迟 | `pg_stat_replication` | > 30 s |
| WAL 生成量 | `pg_stat_wal` | 突增 3 倍 |
| 磁盘使用 | OS | > 80% |
| 事务回滚率 | `pg_stat_database` | 突增 |
| 死锁 | 日志 | 出现即告警 |

### 5.10 连接与安全

- **连接池**：应用前挂 PgBouncer（transaction 模式），把连接数控制在几十条。
- **角色分离**：`app_rw`（读写，无 DDL）、`app_ro`（只读，给分析）、`migrator`（DDL，仅发布时用）。
- **认证**：`scram-sha-256` + TLS；`pg_hba.conf` 只允许内网网段。
- **密钥**：生产不用 `.env` 明文密码，走密钥管理或环境变量注入。
- **审计**：开 `log_connections` / `log_disconnections`；敏感操作走审计日志。

### 5.11 上线检查清单（Runbook）

1. 安装指定小版本，`initdb` 指定 `--encoding=UTF8 --locale=C`。
2. 写入 5.4 的参数，重启生效，`SHOW` 逐项核对。
3. 建角色与库，配置 `pg_hba` 与 TLS。
4. `alembic upgrade head`，校验 `alembic_version` 与代码版本一致。
5. 导入历史数据，跑验收 SQL（行数、时间范围、约束违规为 0）。
6. 配置 WAL 归档，做一次 PITR 演练并记录耗时。
7. 接监控与告警，跑一次慢查询基线。
8. 用真实起点做一次端到端预测，核对幂等（重复运行行数不变）。

### 5.12 什么时候才引入 TimescaleDB

触发条件（满足任意两条再评估）：

- 单表超过 5,000 万行，或分区维护已需要人工干预；
- 连续聚合类查询（按小时/日汇总）成为高频负载；
- 历史数据压缩能带来明确的存储收益；
- 实测证明普通索引 + 分区已无法满足查询时延。

迁移要点：只转大表；`chunk_time_interval` 从 7 天起步；唯一索引必须包含时间列；hypertable 之间的外键有限制，必要时改用应用层校验；先在备库验证，再切换。

## 六、与现有代码的衔接（待办）

| 待办 | 说明 |
|---|---|
| `health()` 版本检查 | 从硬编码 `18.6` 改为"主版本 18，允许小版本差异" |
| NWP 批次入库 | `nwp_revisions` 已建表，需接导入脚本与"按起点选版本"查询 |
| 预测输入追溯 | `predictions` 增加批次/快照标识，能回答"这条预测用了哪批数据" |
| 观测到达时间 | `power_observations` 增加 `received_at`，支持迟到与补数 |
| 只读副本 | 分析类查询走备库，避免影响写入 |

## 七、风险与未决

1. **版本漂移**：代码要求 18.6、运行 18.7，已在多处造成困惑；必须统一为"18.x"。
2. **19 的诱惑**：19 即将 GA，但首个版本不适合直接上生产，等 19.1。
3. **真实数据的时间语义**：如果来源同时提供起报与发布时间，必须分别落库，否则回放不可信。
4. **限电数据缺失**：真实生产里低功率常是人为限电，没有标记会系统性高估——这比数据库选型更影响预测质量。
5. **备份没演练等于没有**：恢复演练必须排进例行工作。

## 八、参考

- PostgreSQL 18.7 发布公告（2026-11-12）：https://www.postgresql.org/about/news/postgresql-187-1712-1616-1520-1425-and-19-rc-2-released-3442/
- PostgreSQL 文档：约束与 `ON CONFLICT`（https://www.postgresql.org/docs/18/ddl-constraints.html、https://www.postgresql.org/docs/18/sql-insert.html）
- TimescaleDB 文档：hypertable 与限制（https://www.tigerdata.com/docs/learn/hypertables/understand-hypertables、https://www.tigerdata.com/docs/deploy/limitations）
- DuckDB 并发模型（2.0）：https://duckdb.org/docs/2.0/connect/concurrency
- ClickHouse 事务说明：https://clickhouse.com/docs/concepts/features/operations/insert/transactions
- InfluxDB 3 版本选择：https://docs.influxdata.com/influxdb3/which-influxdb-3/
- MongoDB 时序集合限制：https://www.mongodb.com/docs/manual/core/timeseries/timeseries-limitations/
- Redis 持久化：https://redis.io/docs/latest/operate/oss_and_stack/management/persistence/
- Parquet 概览：https://parquet.apache.org/docs/overview/
- 本项目：`src/windpower/db/schema.py`、`src/windpower/db/core.py`、`alembic/versions/0002_nwp_revisions.py`、`docs/realtime-readiness.md`

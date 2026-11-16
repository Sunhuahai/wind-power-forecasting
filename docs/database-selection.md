# 数据库调研报告：选型与生产部署（2026-11-16）

> **结论先行**
> 1. **生产主库 = PostgreSQL 18.6 + TimescaleDB 2.32.x**。TimescaleDB 是 PostgreSQL 扩展，不是另一个数据库：事务、外键、JSONB、索引、备份、HA、ORM 全部沿用，额外多出自动分块、列存压缩、增量物化视图、保留策略、时序函数。
> 2. **应用代码一行不改**。只用结构化数据管关系、管实验、管回滚，原始文件与模型权重留在文件系统。
> 3. **现在不迁**：用户 2026-11-16 决定等真实数据到位再动，迁移草案已备好（[timescaledb-migration-draft.sql](timescaledb-migration-draft.sql)），不注册为 Alembic 版本，不会被 `alembic upgrade head` 执行。
>
> 本文合并了三份材料：数据库选型调研、TimescaleDB 深度评估、以及一份「PostgreSQL-only」方案的评审。机制细节保留在附录 [timescaledb-evaluation.md](timescaledb-evaluation.md)；本文自足，只读这一份也能做决定。
> 依据：官方文档与 release notes（2026-11-16 查阅）+ 本项目仓库代码与实测数据。功能比较来自官方资料，"是否适合我们"是工程判断，不是跑分结论。

---

## 一页读完

| 问题 | 答案 |
|---|---|
| 主库选谁 | PostgreSQL 18.6 + TimescaleDB 2.32.x（Linux 部署） |
| 为什么不换 MySQL | 能力够，但要重写数据访问层，且没有"时序能力平滑演进"的路线 |
| 为什么不用 ClickHouse / InfluxDB 当主库 | 会丢掉事务、外键、约束与成熟工具链；它们适合当分析副库/遥测库 |
| 为什么不选 DuckDB 当主库 | 单进程写入，不能做在线服务，适合离线分析 |
| 我们的实测收益 | 列存+压缩代理实测：NWP 49MB → **4.65MB**，预测 13MB → **1.50MB**，功率 13MB → **1.42MB**，约 −90% |
| 生产口径评分 | TimescaleDB **93.6** vs 裸 PostgreSQL **83.2**（同权重下比较，见第三节） |
| 最大的坑 | 版本耦合：2.32.0 只支持到 PG **18.6**，本机 18.7 装不上；升级顺序"先扩展、再 PG" |
| 许可证 | 自建自用免费；压缩/连续聚合/保留策略在 TSL 侧，**不能把数据库能力当服务卖** |
| 现在做什么 | 什么都不做，等真实数据；启动条件见第十节 |

---

## 一、需求画像

| 维度 | 项目现状（实测） | 对选型的影响 |
|---|---|---|
| 数据规模 | 功率观测 175,440 行、NWP 175,440 行、预测 127,296 行、实验 30 条，整库 **115 MB** | 现在很小；但增长曲线由 NWP 与预测明细决定（见第四节） |
| 数据关系 | 功率属场站、预测属实验、实验关联模型与配置 | 需要关系模型与外键 |
| 写入模式 | 批量导入 + 每日预测回写，重复导入要幂等 | 需要唯一键 + `ON CONFLICT` |
| 查询模式 | 按时间窗取历史、按场站/提前量聚合误差、按年限清历史 | 需要时间索引、分区裁剪、按块删除 |
| 历史回放 | 要能回答"当时那一刻，哪些预报真的可用" | 需要发布/到达/有效三种时间 |
| 实验追溯 | 每条预测要能追到模型、参数、数据版本 | 需要实验表与输入版本标识 |
| 实时预测（在准备） | 每日出一次 24 小时预报，10 站 | 需要并发连接、备份、可恢复 |
| 部署环境 | **不在 Mac 上部署**，目标是 Linux（云主机或自有服务器） | 可以按生产标准选型，不必迁就本机限制 |

一句话：**要的是"能管好数据关系、能保证重复写入可控、能追溯实验、还能随数据长大而不停机重构"的库**。

---

## 二、候选清单与内核机制

### 2.1 可以作为主库的候选

| 数据库 | 内核与关键机制 | 主要代价 | 适配度（生产口径） | 结论 |
|---|---|---|---|---|
| **TimescaleDB**（PG 扩展） | hypertable 自动按时间切 chunk + 分区裁剪；hypercore 列存压缩（delta/delta-of-delta/RLE/dictionary/XOR）；连续聚合增量刷新；`DROP CHUNK` 保留策略；`time_bucket_gapfill`/`locf`/`time_weight` | 多一个扩展版本要跟；唯一索引必须含分区列；hypertable 间外键不支持；TSL 许可限制 | **93.6** | **生产主库** |
| PostgreSQL（裸） | MVCC、WAL、主外键与 CHECK、JSONB + GIN、原生分区、`ON CONFLICT`、流复制 | 分区要 pg_partman 或手工维护；无列存、无增量物化视图；大范围聚合要扫全部行 | 83.2 | 同一主库的"不含扩展"形态 |
| MySQL / InnoDB | 聚簇索引、undo log 就地更新、成熟主从 | 分区表不支持外键、唯一索引必须含分区键；无 BRIN；无列存；无增量物化视图 | 70.0 | 合格备选，无迁移理由 |
| SQLite | 单文件、WAL 一写多读 | 库级写锁，多进程并发回写报 `SQLITE_BUSY`；无网络协议、无复制 | 50.4 | 本地工具/边缘缓存 |

### 2.2 更适合特定任务的候选

| 数据库 | 最擅长 | 与我们主库需求的差距 | 结论 |
|---|---|---|---|
| ClickHouse | 百亿行级列式扫描聚合 | 弱事务、`ReplacingMergeTree` 异步去重（合并前读到重复）、无标准外键、小批量点查易打满 CPU | 将来的分析副库 |
| DuckDB | 进程内列式分析、直读 Parquet、与 Polars/Arrow 零拷贝 | 单进程读写、乐观并发，不能当在线服务 | 离线训练与特征探索 |
| InfluxDB 3 | 设备遥测、高基数指标写入 | 无完整 ACID 与外键；底层不可变 Parquet，幂等覆盖订正不是它擅长的 | 独立设备监控再评估 |
| MongoDB | 文档模型 | 我们有明确关系；时序集合对唯一索引与更新有限制 | 无必要 |
| Redis | 亚毫秒缓存 | 内存昂贵，无多维时间范围检索与关联统计 | 接口压力大时做缓存 |
| Parquet | 列式文件格式（非数据库） | 无事务、无约束、无服务 | 检查导出与数据快照 |

### 2.3 一句话解释"为什么 TimescaleDB 同时赢两头"

- 对 MySQL/SQLite：它是 PostgreSQL，关系能力一项不少，多出时序层。
- 对 ClickHouse/InfluxDB：它有事务、外键、约束、JSONB 和成熟工具链，多出的时序能力不是用关系能力换来的。
- **它不是"另一个数据库"，所以不存在"现在用 PG、以后换库"这一步。**

---

## 三、加权评分：两个口径

### 3.1 旧口径（当前规模、本机形态）

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

当时的前提是"只有 115MB、10 个站、在 Mac 上跑"，所以把运维复杂度和迁移成本压得很重。

### 3.2 生产口径（用户明确不在 Mac 部署后重打）

| 维度 | 旧 → 新 | 调整理由 |
|---|---:|---|
| 关系与完整性 | 20 → 18 | 仍是最硬要求，候选间差距小 |
| 幂等写入 / 事务 | 15 → 12 | 同上 |
| **时序能力（分块/压缩/保留）** | 15 → **22** | 生产是多年、多批次、多站的增长负载，从"锦上添花"变成"三年后是否要停机重构" |
| **分析聚合** | 10 → **12** | 看板与误差报表会变成常规负载 |
| 实时写入 | 10 → 8 | 写入量不大，不是瓶颈 |
| 实验追溯 | 10 → 8 | 关系模型就够 |
| **迁移与生态** | 10 → **12** | 生产要备份、HA、监控、ORM；换库等于换一整套工具 |
| **运维复杂度** | 10 → **8** | TimescaleDB 与 PG 共用同一套运维，边际成本远小于换库 |

| 数据库 | 生产口径得分 |
|---|---:|
| **TimescaleDB** | **93.6** |
| PostgreSQL（裸） | 83.2 |
| MySQL | 70.0 |
| DuckDB | 55.6 |
| ClickHouse | 55.6 |
| InfluxDB 3 | 50.8 |
| SQLite | 50.4 |
| MongoDB | 50.0 |
| Redis | 40.4 |

> 分数用于比较"适配我们"，不是性能排名；差 10 分不等于差 10 倍。真正的取舍只有两条：**版本耦合**与**许可证**。

---

## 四、TimescaleDB 相对其他关系型数据库的优势（逐项核实）

每条按「机制 → 对我们意味着什么 → 证据等级 → 代价」写。证据等级：**实测**＝本项目内测过；**官方**＝官方文档明确写；**推断**＝按机制推的，没测。

### 4.1 自动时间分块（hypertable / chunk）

**机制**：建表声明时间列，自动切成按时间范围划分的物理子表（chunk），后续 chunk 自动创建；查询带时间条件时规划器只扫相关 chunk，每个 chunk 有独立索引。

**对比原生 PG**：声明式分区要自己预建（或 pg_partman 定时建），分区数一多规划变慢，`DETACH/DROP PARTITION` 要自己编排。
**对比 MySQL**：同样要手工维护，且分区表不支持外键、唯一索引必须含分区键。

**对我们的意义**：预测与 NWP 都按时间增长、查询都带时间窗，裁剪直接生效；删除老数据是 `DROP CHUNK`（瞬时、不产生死元组）；插入不会因为"整表索引放不进内存"而抖。

**定容口径（官方）**：活跃 chunk 的索引应占内存 25% 以内；默认 chunk 间隔 7 天，按实际索引增长调。

**证据等级**：官方。本项目未真机实测（本机 PG 18.7 不在扩展支持范围）。
**代价**：间隔设太小 → chunk 爆炸、规划变慢；设太大 → 裁剪粗、删除不灵活。

### 4.2 冷数据列存压缩（hypercore）

**机制**：chunk 到龄整块转列存——同列值连续存放成数组，按类型选编码（整数/时间用 delta、delta-of-delta、simple-8b、RLE；低基数列用字典；浮点用 XOR），压缩后仍可直接 SQL 查询（向量化扫描），并用 `segmentby`（按场站分组）+ `orderby`（按时间排序）提速。

**对比原生 PG**：TOAST 是行级压缩，读一行解整行，不能只解一列，也不按类型选编码。
**对比 MySQL**：页级压缩，同样做不到列裁剪与谓词下推。

**本项目实测（代理测量）**：三张大表导出后写成 Parquet（zstd，等价"列存 + 通用压缩"）：

| 表 | 行数 | PG 堆 | PG 索引 | 列存+压缩（实测） | 相对堆 |
|---|---:|---:|---:|---:|---:|
| `nwp_forecasts` | 175,440 | 49 MB | 7.4 MB | **4.65 MB** | −90.5% |
| `predictions` | 127,296 | 13 MB | **17 MB** | **1.50 MB** | −88.5% |
| `power_observations` | 175,440 | 13 MB | 7.4 MB | **1.42 MB** | −89.1% |

注意 `predictions` 的索引（17MB）比数据本身（13MB）还大——列存把这块 secondary index 开销直接消掉，整体收益比表内数字更大。官方口径是"最高约 90%"，与实测同量级。

**对我们的意义**：三年数据不删也放得下；备份/恢复体积降一个量级；大范围误差统计扫描字节数少一个量级。

**证据等级**：实测（体积，代理方法）+ 官方（编码器与压缩率口径）。

**代价（必须写清）**：
1. 压缩 chunk 上零散 `UPDATE`/`DELETE` 代价高：小改会解压一小段，大改要先转回行存、改完再转回（官方文档明确此流程）。
2. 改表结构要先停压缩策略。
3. **对我们的直接风险**：NWP 迟到修订若落进已压缩老 chunk 就踩第 1 条。对策：**压缩年龄阈值必须大于最大修订延迟**（起步 90 天），并用 `nwp_revisions` 的 `received_at - valid_at` 分布定期复核。

### 4.3 连续聚合（continuous aggregate）

**机制**：把聚合查询注册成连续聚合，后台按策略增量刷新，只重算被新数据影响的时间桶（watermark + 失效区间）；"实时聚合"把尚未物化的最近一段与已物化结果 union，查询总能看到最新。

**对比原生 PG**：`REFRESH MATERIALIZED VIEW` 全量重算且默认锁表，历史一大就不可用；要做增量得自己写触发器/作业，容易写错。
**对比 ClickHouse**：物化视图只在写入时算，补历史、改口径都要重建，也不能"原始 + 物化"实时合并。

**对我们的意义**：分站×分提前量×按周的 RMSE/MAE/样本数/缺测率、NWP 版本对比、可用率，全都能预聚合成一到两张视图，看板不再扫千万行。

**证据等级**：官方。
**代价**：查询有限制（不能随意窗口函数/自连接），口径变更要重建；刷新窗口没配好会拖住后台作业。

### 4.4 保留策略与作业调度

**机制**：`add_retention_policy` 到期直接 `DROP CHUNK`；`add_columnstore_policy` 自动转列存；`add_continuous_aggregate_policy` 自动刷新。作业由后台 worker 执行，有运行历史（`timescaledb_information.job_stats`）。

**对比其他关系库**：都得靠外部 cron + 脚本 + 自己加锁防重入，失败无人知；删老数据靠 `DELETE`（写放大、膨胀、要 VACUUM 追赶）。

**对我们的意义**："NWP 留 2 年、功率留 5 年、预测留 3 年"这类规则用 `DROP CHUNK` 实现；作业历史直接当审计线索。

**代价**：库内作业要监控；`DROP CHUNK` 不可撤销，年龄要保守。

### 4.5 时序函数（gapfill / locf / time_weight）

**机制**：`time_bucket` 任意粒度分桶；`time_bucket_gapfill` 生成连续时间轴（缺桶补 NULL）；`locf` 用上一个有效值填充；`time_weight` 做时间加权平均。

**对比原生 PG / MySQL**：都要手写窗口函数 + `generate_series` 左连接，长查询难维护，且容易把"缺测"和"真 0"混成一件事。

**对我们的意义（很具体）**：
- 功率观测有缺测，现在特征构造在 Python 里用字典查，缺了就丢整行；SQL 侧可先 gapfill 对齐。
- 15 分钟升小时，正确做法是时间加权（`time_weight`），简单平均在缺测时有偏。
- `locf` 只用过去值，方向天然安全，不会把未来信息带进特征；但脚本里仍要显式声明"只向前填充"，保持数据使用声明可审计。

**证据等级**：官方（函数与语义）。

### 4.6 查询侧增强（SkipScan / chunk skipping / 向量化聚合）

- **SkipScan**：优化 `SELECT DISTINCT`（如"有哪些 run_id"）。注意 PG 18 本身也有 B-tree skip scan，这项优势在收窄。
- **chunk skipping**（2.17+）：非时间列过滤也能跳块，需显式开启，适合 `source`、`run_id` 这类稀疏维度。
- **向量化聚合**：列存上按批处理，减少调用与解压开销。

**对我们的意义**：顺带变快，不是决定因素。**别期待换库让训练变快**——现在训练取数瓶颈在 Python 逐行特征循环，不在 SQL。

### 4.7 运维连续性：不新增技能栈

- 备份：`pgBackRest`/`pg_dump`/WAL 归档照旧；恢复前后跑 `timescaledb_pre_restore()` / `timescaledb_post_restore()`。
- HA：PG 原生流复制 + Patroni/repmgr；官方**不推荐逻辑复制**同步 TimescaleDB。
- 迁移工具：Alembic、SQLAlchemy、psycopg 全沿用；`db/core.py`、`models.py`、迁移脚本不用改。

**代价**：多一个扩展版本要跟；扩展必须写进 `shared_preload_libraries`。

### 4.8 版本耦合：唯一实质性的新增运维负担

- TimescaleDB **2.32.0**（2026-11-12 发布）兼容上限：**PG 18.6 / 17.11 / 16.15**。
- 发布说明明确：要升更高小版本，**先把扩展升到 2.32.1，再升 PG**。
- 本机是 **PG 18.7**，因此**本机装不了 2.32.0**，这也是没做真机压测的原因。

---

## 五、生产部署方案（Linux）

### 5.1 版本矩阵（先定版本，再装机）

| 组合 | 状态 | 建议 |
|---|---|---|
| PG **18.6** + TimescaleDB **2.32.0** | 官方兼容 | **生产选这个** |
| PG 18.7 + 2.32.0 | 不兼容 | 不要用；等 2.32.1 |
| PG 17.11 + 2.32.0 | 官方兼容 | 保守选择 |
| PG 16.15 + 2.32.0 | 官方兼容 | 只在已有 16 环境时选 |

升级铁律：**先升 TimescaleDB，再升 PostgreSQL**。19 计划 2026-11-19 GA，不上首版，等 19.1。

### 5.2 部署形态（三档）

**A. 单机（开发/预发）**：镜像 `timescale/timescaledb-ha:pg18`（官方生产级镜像），数据卷持久化，端口不对公网，`shared_preload_libraries` 含 `timescaledb`。只做演练验收。

**B. 主备 + 自动切换（生产推荐起步）**：1 主 1 备流复制 + Patroni/repmgr；WAL 归档对象存储（pgBackRest），保留 14–30 天支持 PITR；每天全量 + 持续 WAL；每月恢复演练。目标 RPO ≤ 5 分钟（异步）或 0（同步），RTO ≤ 1 分钟。

**C. Kubernetes（已有集群时）**：CloudNativePG / StackGres / Zalando 都能跑 TimescaleDB 镜像；备份、PVC 扩容、升级编排比裸机复杂，没有 K8s 经验就选 B。

### 5.3 容量规划（估算，非实测上限）

| 场景 | 每年行数 | 存储估算 |
|---|---:|---:|
| 当前：10 站 × 每小时 | 87,600（功率）+ 87,600（NWP） | 整库约 115 MB |
| 100 站 × 15 分钟 | 3,504,000 | 0.5–1 GB/年 |
| NWP 多批次：10 站 × 4 批/日 × 72 目标时刻 | 1,051,200 | 0.3 GB/年 |
| 预测：10 站 × 1 次/日 × 24 小时 × 3 版本 | 262,800 | 0.1 GB/年 |

**增长最快的是 NWP 与预测明细**，不是功率观测。开列存压缩后，三年数据单机 200 GB SSD 绰绰有余。

### 5.4 服务器与参数（16 GB 内存 / 4 vCPU / SSD）

| 参数 | 建议值 | 理由 |
|---|---|---|
| `shared_preload_libraries` | `timescaledb` | 扩展必需，改完重启 |
| `shared_buffers` | 4 GB（25%） | 常规起点 |
| `effective_cache_size` | 12 GB（~70%） | 给规划器"OS 也有缓存"的信号 |
| `maintenance_work_mem` | 1 GB | 建索引、压缩作业更快 |
| `work_mem` | 16–32 MB | 报表聚合/排序够用 |
| `max_worker_processes` | 16 起 | 后台作业要 worker |
| `max_parallel_workers_per_gather` | 4 | 列存扫描可并行 |
| `wal_level` | `replica` | 流复制 + PITR |
| `max_wal_size` | 8 GB | 减少检查点抖动 |
| `checkpoint_completion_target` | 0.9 | 平滑写盘 |
| `random_page_cost` | 1.1 | SSD |
| `effective_io_concurrency` | 200 | SSD |
| `timezone` | `UTC` | 统一 UTC 存、展示层转换 |
| `log_min_duration_statement` | 200 ms | 慢查询留痕 |
| `idle_in_transaction_session_timeout` | 60 s | 防长事务拖住 VACUUM |
| `statement_timeout`（应用角色） | 30 s | 防误查拖垮库 |
| `timescaledb.telemetry_level` | `off`（按合规） | 自建通常不外发遥测 |

安装后先跑 `timescaledb-tune`，再按上表覆盖。

**表级设置（高频 upsert 的行存 chunk 仍要设）**：

```sql
ALTER TABLE predictions SET (
    fillfactor = 90,
    autovacuum_vacuum_scale_factor = 0.02,   -- 默认 0.2
    autovacuum_analyze_scale_factor = 0.01
);
CREATE INDEX IF NOT EXISTS ix_nwp_revisions_received_brin
    ON nwp_revisions USING BRIN (received_at);
```

### 5.5 对象设计：这些表怎么改造

原则：**时间序列大表转 hypertable，小维表/实验表保持普通表**。生产新表的时间列一律用 `TIMESTAMPTZ`（官方对时间维度列的首选建议）；现有开发库是 `timestamp`（不带时区），迁移时改类型，不要在应用层心算时区。

| 表 | 处理 | 时间分区键 | 说明 |
|---|---|---|---|
| `power_observations` | hypertable | `observed_at` | PK `(farm_id, observed_at)` 已含时间列 |
| `nwp_forecasts` | hypertable | `valid_at` | PK `(farm_id, valid_at)` 满足要求 |
| `nwp_revisions` | hypertable | `valid_at` | PK `(farm_id, issued_at, valid_at)` 含时间列；`source` 可做 chunk skipping |
| `predictions` | hypertable | `valid_at` | PK `(run_id, origin, valid_at)` 含时间列 |
| `wind_farms` / `model_runs` / `dataset_splits` | **保持普通表** | — | hypertable 间外键不支持，必须留一侧为普通表 |

四条硬限制（官方，必须提前对齐）：
1. 分区列不能为 NULL。
2. 唯一索引必须包含所有分区列（我们的 PK 都满足）。
3. **不支持把行更新到另一个 chunk**（含 upsert 改了分区列）。现有 `upsert()` 主键含 `valid_at`，只要不改它就没问题——这条写进代码注释。
4. hypertable 之间不能建外键；我们的外键都指向普通表，满足。

**分区策略：只按时间分区，不加 farm_id 哈希空间维度**。10–100 个站的站内查询用普通索引 + 裁剪就够了；加空间维度会让 chunk 数乘以分区数，得不偿失。

DDL 草案（写成幂等迁移）：

```sql
CREATE EXTENSION IF NOT EXISTS timescaledb;

SELECT create_hypertable('power_observations', by_range('observed_at'),
                         chunk_time_interval => INTERVAL '30 days', migrate_data => true);
SELECT create_hypertable('nwp_forecasts', by_range('valid_at'),
                         chunk_time_interval => INTERVAL '7 days', migrate_data => true);
SELECT create_hypertable('nwp_revisions', by_range('valid_at'),
                         chunk_time_interval => INTERVAL '7 days', migrate_data => true);
SELECT create_hypertable('predictions', by_range('valid_at'),
                         chunk_time_interval => INTERVAL '7 days', migrate_data => true);

-- 冷数据转列存；阈值必须大于最大修订延迟（起步 90 天）
ALTER TABLE nwp_revisions SET (timescaledb.compress = true);
SELECT add_columnstore_policy('nwp_revisions', INTERVAL '90 days');
```

完整版（前置检查、保留策略、连续聚合、迁移后校验）见 [timescaledb-migration-draft.sql](timescaledb-migration-draft.sql)。

### 5.6 迁移步骤（现有 PG → TimescaleDB 实例）

1. **冷备**：`pg_dump -Fc` 全量（迁移前唯一可靠退路）。
2. **起新实例**：PG 18.6 + TimescaleDB 2.32.0，`CREATE EXTENSION timescaledb`。
3. **建结构**：Alembic 迁到最新（先不加 hypertable），再跑 `create_hypertable`。
4. **灌数据**：`pg_restore` 先普通表后大表；大表灌完再 `create_hypertable(migrate_data => true)`，比"先建超表再逐行灌"快一个量级。
5. **校验**：行数、`sum(power)`、`count(distinct farm_id)`、时间范围四项逐表比对；再抽 100 行随机比对。
6. **切应用**：改 `DATABASE_URL`，先只读验证再切写入。
7. **观察一周**：作业历史、chunk 数、压缩率、查询 P95。

**回滚**：TimescaleDB 没有官方"撤销 hypertable"路径，只能靠第 1 步恢复，或切回旧实例。所以第 1 步不是可选项。
**逻辑复制不可用**：官方不推荐，所以"边同步边切换"行不通，必须接受一次短暂停机（数据量小，10 分钟级）。

### 5.7 备份 / 高可用 / 监控 / 安全

| 项 | 方案 |
|---|---|
| 每日全量 | `pg_dump -Fc` 或 `pg_basebackup` 到对象存储 |
| 连续归档 | WAL 归档，支持 PITR；RPO ≤ 5 分钟，RTO ≤ 1 小时 |
| 演练 | 每月一次恢复演练，记录实际耗时 |
| 高可用 | 主 + 流复制备库，Patroni 自动切换，注意 fencing 防脑裂 |
| 连接池 | 应用前挂 PgBouncer（transaction 模式） |
| 角色分离 | `app_rw`（无 DDL）、`app_ro`（分析）、`migrator`（仅发布时） |
| 认证 | `scram-sha-256` + TLS，`pg_hba` 只放内网 |
| 密钥 | 不用 `.env` 明文，走密钥管理或环境变量注入 |

告警阈值：

| 指标 | 来源 | 阈值 |
|---|---|---|
| 后台作业失败 | `timescaledb_information.job_stats` | 任一 `last_run_status <> 'Success'` 立即告警 |
| 连续聚合滞后 | `job_stats.last_successful_finish` | > 2 个刷新周期 |
| chunk 数量 | `timescaledb_information.chunks` | 单表 > 500 检查间隔 |
| 压缩率 | `hypertable_columnstore_stats` | 低于 70% 检查 `segmentby/orderby` |
| 复制延迟 | `pg_stat_replication` | > 30 s |
| 慢查询 | `pg_stat_statements` | 单条 > 2 s 或均值 > 200 ms |
| 连接数 | `pg_stat_activity` | > 80% `max_connections` |
| 磁盘 | OS | > 80% |

### 5.8 上线检查清单（Runbook）

1. 装指定小版本，`initdb --encoding=UTF8 --locale=C`。
2. 写入 5.4 参数，重启，`SHOW` 逐项核对。
3. 建角色与库，配 `pg_hba` 与 TLS。
4. `CREATE EXTENSION timescaledb`（超级用户），再 `alembic upgrade head`。
5. 转 hypertable（5.5 DDL），导入历史，跑验收 SQL（行数、时间范围、约束违规为 0）。
6. 配 WAL 归档，做一次 PITR 演练并记录耗时。
7. 接监控告警，跑一次慢查询基线。
8. 用真实起点做端到端预测，核对幂等（重复运行行数不变）。

---

## 六、许可证与合规（别踩坑）

| 版本 | 许可 | 有什么 | 能否对外提供数据库服务 |
|---|---|---|---|
| Apache 2 版 | Apache 2.0 | hypertable、chunk、`time_bucket`、基本函数 | 可以 |
| **Community 版（TSL）** | Timescale License | 上面全部 **+ 列存压缩、连续聚合、保留策略、作业、gapfill/locf/time_weight、SkipScan** | **不可以** |

- **自建自用完全免费**，可改源码、可跑生产。
- 不能把 TSL 版**作为数据库服务对外售卖**。我们内部用（自己出预报、自己看板）没问题。
- 将来若交付"给客户的平台"且客户能直接用到数据库能力，需法务确认边界。
- 交付形态建议：应用镜像与数据库分开部署，客户自己持有数据库实例，避免把"数据库服务"打包进产品。

---

## 七、评审另一版「PostgreSQL-only」报告（2026-11-16 收到）

那份报告把项目包袱剥离、按通用风电业务来写，结论是"PostgreSQL 唯一主库，TimescaleDB 等亿级再挂"。内核对比（B-Tree vs LSM vs 列存、MVCC 膨胀、BRIN、`ReplacingMergeTree` 异步去重）写得扎实，三条工程细节已采纳；但有**两处事实错误**和**一处结构性误判**。

### 7.1 采纳的部分

1. 时间列统一 `TIMESTAMPTZ`（已写进 5.5）。
2. 入库时间建 BRIN（已写进 5.4）。
3. 高频 upsert 表 `fillfactor=90` + 激进 autovacuum（已写进 5.4）。
4. 模型权重存文件系统 + `sha256`、库里只存路径——我们 `models/wind_v1/manifest.json` 本来就是这形态，不用改。

### 7.2 事实错误一：规模估算差约 5 倍，还漏了最大的一张表

它写"50 场站、2000 台风机，场站级数据年增量仅千万行级"。实算：

| 口径 | 算式 | 每年行数 |
|---|---|---:|
| 场站级功率（15 分钟） | 50 × 96 × 365 | **175 万**（百万级，不是千万级） |
| 机组级（2000 台 × 96 点） | 2000 × 96 × 365 | 7,000 万 |
| **NWP（50 站 × 4 次/天 × 72 提前量）** | 50 × 4 × 72 × 365 | **526 万** |

关键在最后一行：**预测系统里行数最大的通常是 NWP，不是功率观测**，是它的 3 倍。而 NWP 与预测明细正是"按时间查、按时间删、按时间压"最典型的表。用"观测才百万级、不用着急"来推迟时序层，等于看错了增长最快的那张表。

### 7.3 事实错误二：「以后平滑挂载 TimescaleDB」在它自己的设计下不成立

它给三张时序表设计了原生 `PARTITION BY RANGE` 月分区，又主张以后 `CREATE EXTENSION` 升级。两者冲突，官方 `create_hypertable()` 文档原文：

> You cannot run `create_hypertable()` on a table that is already partitioned using declarative partitioning or inheritance.

（[TimescaleDB 参考文档](https://www.tigerdata.com/docs/reference/timescaledb/hypertables)）

按那份设计落地后想换，得先拆分区、重建表、重灌数据。**"以后再挂"不是加个扩展，是一次停机迁移**；再叠加"官方不推荐逻辑复制"和"hypertable 没有撤销路径"，这个"以后"的成本被严重低估。

### 7.4 结构性误判：把 TimescaleDB 归到"专用时序库"

它把 TimescaleDB 与 InfluxDB、ClickHouse 并列，于是推出"等规模大了再说"。但 TimescaleDB 是 **PostgreSQL 扩展**，事务、外键、JSONB、备份、HA、ORM 全在。**它和 PostgreSQL 不是二选一**——"现在装"与"以后装"的真实差异只有版本耦合与许可证。这两条它一个字没提，而那才是选它真正要认下的代价。

### 7.5 顺带纠正四个设计细节

| 它的写法 | 问题 | 建议 |
|---|---|---|
| `wind_dir_100m` 存 0~360 角度 | 角度在 0/360 处不连续，模型与统计都会踩 | 存 `u100/v100` 分量（本项目已是如此），或 sin/cos 编码 |
| `air_density` 独立字段 | 派生量，易与温度/气压不一致 | 用 `GENERATED ALWAYS AS` 生成列，或入库时统一算 |
| `COPY (...) TO PROGRAM` 导 Parquet | 依赖 shell、权限面大、失败难查 | `COPY TO STDOUT` 交给导出程序，或 DuckDB `postgres_scanner` 只读扫 |
| 月分区 + 手写冷热导出脚本 | 脚本要自己维护、失败无人知 | 用 `add_columnstore_policy` + `add_retention_policy` 替代 |

---

## 八、与现有代码的衔接（待办）

| 待办 | 说明 |
|---|---|
| `health()` 版本检查 | 从硬编码 `18.6` 改为"主版本 18，允许小版本差异" |
| NWP 批次入库 | `nwp_revisions` 已建表，需接导入脚本与"按起点选版本"查询 |
| 预测输入追溯 | `predictions` 增加批次/快照标识，能回答"这条预测用了哪批数据" |
| 观测到达时间 | `power_observations` 增加 `received_at`，支持迟到与补数 |
| 只读副本 | 分析类查询走备库，避免影响写入 |

---

## 九、风险与未决

1. **版本漂移**：代码要求 18.6、运行 18.7，已在多处造成困惑；统一为"18.x"。
2. **扩展版本耦合**：2.32.0 只到 PG 18.6，生产钉版本，升级顺序写进检查清单。
3. **压缩 vs 迟到修订**：压缩年龄阈值必须大于最大修订延迟，否则老 chunk 被改写代价高。
4. **许可证边界**：TSL 不能当服务卖；将来做交付平台前先确认。
5. **真实数据的时间语义**：来源同时给起报与发布时刻就必须分开落库，否则回放不可信。
6. **限电数据缺失**：真实生产里低功率常是人为限电，没标记会系统性高估——这比数据库选型更影响预测质量。
7. **备份没演练等于没有**：恢复演练必须排进例行工作。

---

## 十、下一步：备而不发

用户 2026-11-16 决定：**先不做真机验证、不迁移，等真实数据到位**。本地开发库继续用裸 PostgreSQL 18.7，不动。

**已备好**
- [timescaledb-migration-draft.sql](timescaledb-migration-draft.sql)：前置检查、四张 `create_hypertable`、列存/保留策略、两张连续聚合、迁移后校验、生产新表规范。**故意不注册为 Alembic 版本**。
- 生产形态已定：PG 18.6 + TimescaleDB 2.32.x；升级顺序"先扩展、再 PG"。

**启动条件（满足任一条就开 P0，不再讨论）**
1. 真实数据接入后日均新增 > 10 万行，或任一表 > 5,000 万行；
2. 需要看板类聚合查询（分站×提前量×按周误差、可用率）；
3. 需要按年限自动清理历史。

**P0｜真机验证（1 天）**：目标 Linux 起 `timescale/timescaledb-ha:pg18`，灌现有三张大表，量迁移耗时、实际压缩率（`hypertable_columnstore_stats`）、5 条典型查询 P50/P95——把本文的"官方口径 + 代理实测"换成"我们的真机数字"。

**P1｜迁移落地（半天）**：草案转 Alembic 幂等迁移，并在 `docs/realtime-readiness.md` 补一节"生产库形态"。

**P2｜策略与报表（1 天）**：开列存与保留策略，接两张连续聚合，采一周作业历史。

**P3｜回滚演练**：用 `pg_dump` 恢复一次，确认 RTO 真实（hypertable 无官方撤销路径，这是唯一退路）。

---

## 十一、参考

- TimescaleDB 2.32.0 release notes（2026-11-12）：兼容 PG 18.6 / 17.11 / 16.15；升级顺序先扩展后 PG
- hypertable 与 chunk 定容：[TimescaleDB 参考文档](https://www.tigerdata.com/docs/reference/timescaledb/hypertables)、[hypertable 优化](https://www.tigerdata.com/docs/learn/hypertables/optimize-data-in-hypertables)（默认 7 天；活跃 chunk 索引 ≤ 内存 25%）
- 列存与编码：官方 *Hypercore* / *Compression methods*（delta、delta-of-delta、simple-8b、RLE、XOR、dictionary；最高约 90%）
- 压缩数据修改限制：官方 *Modify data in hypercore*（大改先转回行存）
- 连续聚合：官方 *Continuous aggregates*
- 限制清单：官方 *Limitations*（分区列非空、唯一索引含分区列、跨 chunk 更新不支持、hypertable 间外键不支持）
- 许可：官方 *Compare TimescaleDB editions*（Apache 2 版 vs TSL Community 版）
- 安装与 HA：官方 *Install on Linux / Kubernetes*、*About high availability*（流复制；不推荐逻辑复制）
- PostgreSQL 18.7 发布公告（2026-11-12）；约束与 `ON CONFLICT` 文档（`/docs/18/ddl-constraints.html`、`/docs/18/sql-insert.html`）
- DuckDB 并发模型、ClickHouse 事务说明、InfluxDB 3 版本选择、MongoDB 时序集合限制、Redis 持久化、Parquet 概览（见附录链接）
- 本项目实测：`pg_stat_user_tables` 行数/堆/索引体积；Parquet/zstd 列存压缩代理测量（2026-11-16）
- 本项目代码：`src/windpower/db/schema.py`、`src/windpower/db/core.py`、`alembic/versions/0002_nwp_revisions.py`、`docs/realtime-readiness.md`、`models/wind_v1/manifest.json`

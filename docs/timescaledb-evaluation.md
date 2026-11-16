# TimescaleDB 深度评估：相对其他关系型数据库的优势（2026-11-16）

> 结论先行：**把生产主库定为「PostgreSQL + TimescaleDB 扩展」**。它不是一个要替换 PostgreSQL 的新数据库，而是 PostgreSQL 的超集——关系模型、事务、外键、JSONB、备份、HA、驱动、ORM 全部沿用，额外多出一层时序能力（自动分块、列存压缩、增量物化视图、保留策略、时序函数）。
>
> 因此「选 TimescaleDB」和「选 PostgreSQL」不是二选一：**生产实例直接发行 TimescaleDB，应用代码一行不改**。代价只有两条——多跟一个扩展版本，以及许可证对「对外提供数据库服务」有限制。
>
> 本文依据：官方文档与 release notes（2026-11-16 查阅）+ 本项目仓库代码与本地实测数据。所有"对我们要不要紧"的判断是工程判断，不是跑分结论。

---

## 一页读完

| 问题 | 答案 |
|---|---|
| 它是什么 | PostgreSQL 扩展（hypertable / hypercore / continuous aggregate），不是独立数据库 |
| 相对 PostgreSQL 多什么 | 自动按时间分块 + 分区裁剪、冷数据列存压缩（官方称最高约 90%）、增量物化视图、按块删除的保留策略、作业调度、`time_bucket_gapfill`/`locf`/`time_weight` |
| 相对 MySQL / SQLite 多什么 | 上面全部；另外 MySQL 的页级压缩不是列存、分区要手工维护、没有增量物化视图；SQLite 单写者、无分区 |
| 相对 ClickHouse / InfluxDB 多什么 | 完整事务、外键、约束、JSONB、无痛沿用 PG 工具链与 ORM（这些库是"分析/遥测专用"，当主库会丢掉业务侧能力） |
| 我们的实测体积收益 | 同一份数据按列存+压缩（Parquet/zstd 代理实测）：NWP 表堆 49MB → 4.65MB，预测表 13MB → 1.50MB，功率表 13MB → 1.42MB，**约 −90%** |
| 最大的坑 | 版本耦合：TimescaleDB 2.32.0 官方只支持到 **PostgreSQL 18.6**，本机 18.7 **不在支持范围**；升级必须"先升扩展、再升 PG" |
| 许可证 | 自建自用免费；压缩/连续聚合/保留策略等核心能力在 Timescale License（TSL）侧，**不能把数据库能力作为服务对外售卖** |

---

## 二、先把概念摆正：它和谁比、怎么比

这份对比容易比错，因为 TimescaleDB 同时属于两类：

- **作为关系型数据库**：它就是一个 PostgreSQL。所以"TimescaleDB vs MySQL/SQLite"比的是 PG 阵营与它们的能力差。
- **作为时序数据库**：它在 PG 上长了时序层。所以"TimescaleDB vs ClickHouse/InfluxDB"比的是"有关系的时序"与"纯分析/遥测"。

这个双重身份正是它的核心优势：**它不需要你在"关系能力"和"时序能力"之间做取舍**。其他候选都被迫取舍：

| 候选 | 被迫放弃的那一半 |
|---|---|
| ClickHouse / InfluxDB | 放弃事务、外键、约束、UPDATE/DELETE 语义与成熟 ORM/迁移工具 |
| MySQL | 放弃 PG 的扩展生态、JSONB 索引与"时序层可平滑叠加"的路线 |
| SQLite | 放弃并发写与服务形态 |
| DuckDB | 放弃长期在线服务与多进程写入 |
| 原生 PostgreSQL | 放弃开箱即用的分块、压缩、增量聚合（要靠 pg_partman + 手写作业补） |

---

## 三、逐项拆解：TimescaleDB 相对其他关系型数据库的优势

每条按「机制 → 对我们意味着什么 → 证据等级 → 代价」写。证据等级：**实测**＝本项目内测过；**官方**＝官方文档明确写；**推断**＝我按机制推的，没测。

### 3.1 自动时间分块（hypertable / chunk）

**机制**：建表时声明时间列，TimescaleDB 把表切成按时间范围划分的物理子表（chunk），后续 chunk 自动创建，无需预先建分区。查询带时间条件时，规划器只扫描相关 chunk（分区裁剪），并且每个 chunk 有独立索引。

**对比原生 PostgreSQL**：PG 的声明式分区需要你自己预建分区（或引入 pg_partman 定时建）、分区过多会让规划时间上升、`DETACH/DROP PARTITION` 要自己编排；TimescaleDB 把这一整套变成后台自动。

**对比 MySQL**：MySQL 分区表同样要手工维护，且分区表对外键、唯一索引有额外限制。

**对我们的意义**：
- 预测表在生产里按"每天多次运行 × 24 小时 × 场站数"增长，天然按时间切片；查询几乎都带时间窗，裁剪直接生效。
- 每个 chunk 的索引小、常驻内存，插入不会因为"整表索引放不进内存"而抖。
- 官方给的定容口径很好用：**活跃 chunk 的索引应占内存 25% 以内**，默认 chunk 间隔 7 天，按实际索引增长调（官方文档）。

**证据等级**：官方（机制与定容口径）。本项目未实测（本地 PG 18.7 不在扩展支持范围）。

**代价**：chunk 间隔设太小 → chunk 数量爆炸、规划变慢；设太大 → 裁剪粒度粗、删除不灵活。上线后要按"每日索引增长"复核一次。

### 3.2 冷数据列存压缩（hypercore / columnstore）

**机制**：chunk 到一定年龄后整块从行存转成列存——同一列的值连续存放成数组，再按类型选编码：整数/时间用 delta、delta-of-delta、simple-8b、RLE；低基数列用字典；浮点用 XOR。压缩后仍可被 SQL 直接查询（向量化扫描），并可用 `segmentby`（按场站分组）+ `orderby`（按时间排序）进一步提速。

**对比原生 PostgreSQL**：PG 的 TOAST 是行级压缩，读一行要解一整行，无法只解某一列，也没有按类型选编码；对大范围聚合没有优势。

**对比 MySQL**：InnoDB 页压缩/表压缩同样是页级，不是列存，不能列裁剪、不能谓词下推。

**本项目实测（代理测量）**：把三张大表导成 CSV 再写成 Parquet（zstd，等价"列存 + 通用压缩"），与 PG 实际占用对比：

| 表 | 行数 | PG 堆大小 | PG 索引 | 列存+压缩（实测） | 相对堆 |
|---|---:|---:|---:|---:|---:|
| `nwp_forecasts` | 175,440 | 49 MB | 7.4 MB | **4.65 MB** | −90.5% |
| `predictions` | 127,296 | 13 MB | 17 MB | **1.50 MB** | −88.5% |
| `power_observations` | 175,440 | 13 MB | 7.4 MB | **1.42 MB** | −89.1% |

说明：Parquet/zstd 只是代理，TimescaleDB 用自家编码器，官方口径是"最高约 90%"。但量级已经说明问题：**预测表的索引比数据本身还大（17MB vs 13MB）**，列存把这块 secondary index 开销直接消掉，整体收益比上表更大。

**对我们的意义**：
- 生产规模下（100 站、15 分钟观测 ≈ 350 万行/年；NWP 每次运行一批，行数再乘运行次数；预测表 8 次/天 × 24h × 100 站 ≈ 700 万行/年），三年数据不删也能放进单机 SSD。
- 备份体积、恢复时间、跨环境拷贝同步下降。
- 大范围误差统计（按年、按场站、按提前量）扫描字节数下降一个量级。

**证据等级**：实测（体积，代理方法）；官方（编码器与压缩率口径）。

**代价（必须写清）**：
1. 压缩后的 chunk 上做零散 `UPDATE`/`DELETE` 代价高：小改会自动解压一小段，大改要先把 chunk 转回行存、改完再转回列存（官方文档明确此流程）。
2. 改表结构要先停掉压缩策略。
3. **对我们的直接风险**：NWP 迟到修订如果落进已压缩的老 chunk，就踩到上面第 1 条。对策很简单——**列存策略的年龄阈值必须大于最大修订延迟**（比如 90 天以上的 chunk 才压缩，迟到修订基本不会超过这个窗口），并把这个假设写进运维手册、定期用 `nwp_revisions` 的 `received_at - valid_at` 分布复核。

### 3.3 连续聚合（continuous aggregate）

**机制**：把一条聚合查询注册成"连续聚合"，后台按策略增量刷新：只重算被新数据影响的时间桶（watermark + 失效区间），不用全量重算。还能"实时聚合"——把尚未物化的最近一段与已物化结果 union，查询永远看到最新数据。

**对比原生 PostgreSQL**：物化视图 `REFRESH MATERIALIZED VIEW` 是全量重算且默认锁表（`CONCURRENTLY` 有额外限制），历史数据一大就不可用；要做增量就得自己写触发器/作业，容易写错。

**对比 ClickHouse**：ClickHouse 的物化视图只在写入时计算，补历史、改口径都要重建，且不支持"原始数据 + 物化结果"的实时合并。

**对我们的意义**：误差报表天然是"预聚合"场景——分站×分提前量×按周/月的 RMSE、MAE、样本数、缺测率；NWP 版本对比；可用率。做成一到两张连续聚合后，看板查询不再扫千万行原始表。

**证据等级**：官方（机制）。未实测。

**代价**：连续聚合的查询有限制（不能随意窗口函数/自连接），口径变更要重建视图；刷新策略窗口没配好会拖住后台作业。

### 3.4 保留策略与作业调度（policies / jobs）

**机制**：`add_retention_policy` 到期直接 `DROP CHUNK`（元数据操作，不产生大量 dead tuple，不需要 VACUUM 追赶）；`add_columnstore_policy` 自动转列存；`add_continuous_aggregate_policy` 自动刷新。所有作业由后台 worker 执行，有运行历史与失败记录（`timescaledb_information.job_stats`）。

**对比其他关系库**：MySQL/PG 都得靠外部 cron + 脚本 + 自己加锁防重入；脚本跑失败没人知道，删老数据靠 `DELETE`（写放大 + 膨胀 + 需要 VACUUM）。

**对我们的意义**：真实生产要"原始 NWP 留 2 年、功率留 5 年、预测留 3 年"这类规则。用 `DROP CHUNK` 实现，删除是瞬时且不膨胀；作业历史直接当审计线索。

**代价**：作业是库内的，要监控（失败、堆积）；`DROP CHUNK` 不可撤销，策略年龄要保守。

### 3.5 时序函数（time_bucket / gapfill / locf / time_weight）

**机制**：`time_bucket` 按任意粒度分桶；`time_bucket_gapfill` 生成连续时间轴（缺的桶补 NULL）；`locf` 用"上一个有效值"填充；`time_weight` 做时间加权平均。

**对比原生 PostgreSQL**：都要手写窗口函数 + `generate_series` 左连接，长查询难维护，且容易把"缺测"和"真 0"混成一件事。

**对我们的意义（这条对我们很具体）**：
- 功率观测有缺测，特征构造现在在 Python 里用字典查 `power.get(t)`，缺了就丢整行；用 `gapfill + locf` 可以在 SQL 侧先对齐成连续时间轴。
- 15 分钟观测升到小时，正确做法是**时间加权平均**（`time_weight`），简单平均在缺测时会有偏。
- 注意：`locf` 只能用过去值，方向天然安全，不会把未来信息带进特征；但仍要在脚本里显式声明"只向前填充"，保持我们数据使用声明的可审计性。

**证据等级**：官方（函数存在与语义）。未实测。

### 3.6 查询侧增强：SkipScan、chunk skipping、向量化聚合

- **SkipScan**：优化 `SELECT DISTINCT` 类查询（如"有哪些 run_id"），没有合适索引也能跳读。注意：PostgreSQL 18 本身新增了 B-tree skip scan，所以这项优势在收窄，只是 TimescaleDB 的 SkipScan 对 DISTINCT 场景更直接。
- **chunk skipping**（2.17+）：为"非时间列"过滤也能跳过整块（需显式开启），稀疏维度（如 `source`、`run_id`）上有效。
- **向量化聚合**：列存上的聚合按批处理，减少函数调用与解压开销。

**对我们的意义**：属于"顺带变快"，不是决定因素。我们的训练取数瓶颈其实在 Python 侧特征循环（一次只查一张表再逐行算），不在 SQL——所以**不要期待换个库让训练变快**，这点必须诚实。

### 3.7 运维连续性：不新增一套技能栈

- 备份：`pgBackRest` / `pg_dump` / WAL 归档全部照旧；TimescaleDB 提供 `timescaledb_pre_restore` / `timescaledb_post_restore` 处理恢复前后的元数据。
- HA：用 PG 原生流复制 + Patroni/repmgr；官方明确**不推荐用逻辑复制**同步 TimescaleDB（需要分区根表 schema 同步）。
- 迁移工具：Alembic、SQLAlchemy、psycopg 全部照旧——我们的 `db/core.py`、`models.py`、迁移脚本不用改。

**对比**：换成 ClickHouse/InfluxDB，等于同时换掉驱动、ORM、迁移工具、备份方案、监控看板、团队排障经验。

**代价**：多一个扩展版本要跟（见 3.8），以及"扩展必须写进 `shared_preload_libraries`"这类 PG 运维细节。

### 3.8 版本耦合：唯一实质性的新增运维负担

- TimescaleDB **2.32.0**（2026-11-12 发布）官方兼容上限是 **PostgreSQL 18.6 / 17.11 / 16.15**。
- 发布说明明确：要升到更高的 PG 小版本，**先把扩展升到 2.32.1，再升 PG**。
- 本机现在是 **PostgreSQL 18.7**，因此**本地这台机器目前装不了 2.32.0**（这也是我没有在本机做真机压测的原因）。

**对我们的部署含义**：生产实例选 **PG 18.6 + TimescaleDB 2.32.x**（或 17.11 LTS 线），把"扩展版本 ↔ PG 小版本"写进升级检查清单；等 2.32.1 出来再考虑 18.7。

---

## 四、横向对比：能力矩阵（相对其他关系型数据库）

图例：●● 完整内置 ／ ● 支持但要自己搭 ／ △ 受限 ／ ✘ 没有。

| 能力 | TimescaleDB | PostgreSQL 18 | MySQL 8.4 | SQLite |
|---|---|---|---|---|
| 事务 / 外键 / CHECK 约束 | ●● | ●● | ●● | ● |
| 按时间自动分块 | ●● | ●（要 pg_partman 或手写） | ●（手工分区，且有外键/唯一索引限制） | ✘ |
| 分区裁剪 / 跳过无关块 | ●● | ●（分区裁剪可用，粒度受手工分区影响） | ● | ✘ |
| 冷数据列存压缩 + 谓词下推 | ●● | ✘（仅行级 TOAST） | ✘（仅页级压缩） | ✘ |
| 增量物化视图 | ●● | ✘（全量 REFRESH） | ✘ | ✘ |
| 按时间保留策略（DROP CHUNK） | ●● | ●（DROP PARTITION） | ●（DROP PARTITION，需手工） | ✘ |
| 时序函数（gapfill/locf/time_weight） | ●● | ✘ | ✘ | ✘ |
| DISTINCT 跳读（SkipScan） | ●● | ●（PG18 新增 B-tree skip scan） | △ | ✘ |
| JSONB + GIN 索引 | ●● | ●● | △（JSON 函数可用，索引能力弱） | ✘ |
| 扩展生态（PostGIS/pgvector 等） | ●●（继承 PG） | ●● | ✘ | ✘ |
| 备份/HA 工具链成熟度 | ●●（继承 PG） | ●● | ●● | △ |
| 并发写（多进程服务） | ●● | ●● | ●● | ✘（单写者） |
| 额外运维心智负担 | 多一个扩展版本 | 无 | 无（但与 PG 生态隔离） | 无 |

一句话：**TimescaleDB 的优势不是"某一项特别强"，而是"这些能力全都在一个 PostgreSQL 里，且不需要为它们放弃关系能力"**。

---

## 五、按生产口径重新打分

上一版文档（[database-selection.md](database-selection.md)）的打分是**当时口径**：把"现在只有 115MB、10 个站、在 Mac 上跑"作为前提，运维复杂度和迁移成本压得很重，所以 PostgreSQL 89 > TimescaleDB 84。

用户已明确：**以后不在 Mac 上部署，按生产环境评估**。权重随之调整，理由逐条写明：

| 维度 | 旧权重 | 新权重 | 调整理由 |
|---|---:|---:|---|
| 关系与完整性 | 20 | 18 | 仍是最硬的要求，但各候选差距小 |
| 幂等写入 / 事务 | 15 | 12 | 同上 |
| **时序能力（分块/压缩/保留）** | 15 | **22** | 生产是"多年、多批次、多站"的增长型负载，这一项从"锦上添花"变成"决定三年后是否要停机重构" |
| **分析聚合** | 10 | **12** | 看板与误差报表会变成常规负载 |
| 实时写入 | 10 | 8 | 我们写入量不大（每天几百到几万行），不是瓶颈 |
| 实验追溯 | 10 | 8 | 关系模型就能满足 |
| **迁移与生态** | 10 | **12** | 生产要备份、HA、监控、ORM、迁移；换库等于换一整套工具 |
| **运维复杂度** | 10 | **8** | TimescaleDB 与 PG 共用同一套运维，边际成本远小于换库 |

重新打分（1–5 分 × 权重，满分 100）：

| 数据库 | 得分 | 定位变化 |
|---|---:|---|
| **TimescaleDB** | **93.6** | **生产主库**（= PostgreSQL + 时序层） |
| PostgreSQL（原生） | 83.2 | 同一主库的"不含扩展"形态；规模小时完全够用 |
| MySQL | 70.0 | 合格备选；换过去要重写数据访问层，且没有时序演进路线 |
| DuckDB | 55.6 | 离线分析工具 |
| ClickHouse | 55.6 | 将来的分析副库（海量明细聚合） |
| InfluxDB 3 | 50.8 | 独立设备监控场景再评估 |
| SQLite | 50.4 | 本地工具 / 边缘缓存 |
| MongoDB | 50.0 | 无必要 |
| Redis | 40.4 | 将来做缓存 |

**注意**：分数差 10 分不等于性能差 10 倍，它反映的是"在权重下谁更省事"。真正的取舍只有两条：**版本耦合**与**许可证**。

---

## 六、许可证与合规（别踩坑）

TimescaleDB 有两个版本（官方文档口径）：

| 版本 | 许可 | 有什么 | 能不能对外提供数据库服务 |
|---|---|---|---|
| Apache 2 版 | Apache 2.0 | hypertable、chunk、`time_bucket`、基本函数 | 可以 |
| **Community 版（TSL）** | Timescale License | 上面全部 **+ 列存压缩、连续聚合、保留策略、作业、gapfill/locf/time_weight、SkipScan** | **不可以** |

要点：
- **自建自用完全免费**，可以改源码、可以跑生产。
- 不能把 TSL 版**作为数据库服务对外售卖**。我们内部用（自己出预报、自己看板）没问题。
- 如果将来把系统做成"给客户交付的平台"，且客户能直接用到数据库能力，需要法务确认边界（是"SaaS 应用"还是"提供数据库服务"）。
- 交付形态建议：应用镜像 + 数据库单独部署，客户自己持有数据库实例，避免把"数据库服务"打包进产品。

---

## 七、生产部署方案（Linux，不依赖 Mac）

### 7.1 版本矩阵（先定版本，再装机）

| 组合 | 状态 | 建议 |
|---|---|---|
| PG **18.6** + TimescaleDB **2.32.0** | 官方兼容 | **生产选这个** |
| PG 18.7 + 2.32.0 | **不兼容** | 不要用；等 2.32.1 |
| PG 17.11 + 2.32.0 | 官方兼容 | 保守选择（17 线更成熟） |
| PG 16.15 + 2.32.0 | 官方兼容 | 只在已有 16 环境时选 |

升级铁律：**先升 TimescaleDB，再升 PostgreSQL**（官方发布说明明确要求）。

### 7.2 部署形态（三档）

**A. 单机（开发/预发）**
- 镜像：`timescale/timescaledb-ha:pg18`（官方推荐的生产级镜像，含 TimescaleDB 与常用扩展）。
- 数据卷持久化，端口不对公网暴露，`shared_preload_libraries` 含 `timescaledb`。
- 只用于演练与验收，不承接生产。

**B. 主备 + 自动切换（生产最小可用，推荐起步）**
- 1 主 1 备（同步或异步流复制，看 RPO 要求）+ Patroni 或 repmgr 管切换。
- WAL 归档到对象存储（pgBackRest），保留 14–30 天，支持 PITR。
- 每天 1 次全量 + 持续 WAL；每月做一次恢复演练（否则备份等于没有）。
- 目标：RPO ≤ 5 分钟（异步）或 0（同步），RTO ≤ 1 分钟（Patroni 自动切换）。

**C. Kubernetes（已有集群时）**
- CloudNativePG / StackGres / Zalando Postgres Operator 都支持 TimescaleDB 镜像。
- 注意：备份、PVC 扩容、升级编排比裸机复杂；没有 K8s 运维经验时，B 档更省事。

### 7.3 参数起点（16GB 内存、4 核、SSD）

| 参数 | 建议值 | 理由 |
|---|---|---|
| `shared_preload_libraries` | `timescaledb` | 扩展必需，改完要重启 |
| `shared_buffers` | 4GB（25%） | 常规起点 |
| `effective_cache_size` | 12GB（~70%） | 给规划器"操作系统也有缓存"的信号 |
| `maintenance_work_mem` | 1GB | 建索引/压缩作业更快 |
| `work_mem` | 16–32MB | 报表查询会做排序/哈希聚合 |
| `max_worker_processes` | 16 起 | 后台作业要 worker |
| `max_parallel_workers_per_gather` | 4 | 列存扫描可并行 |
| `wal_level` | `replica` | 流复制 + PITR |
| `timezone` | `UTC` | 我们的时间列不带时区，统一 UTC 存、展示层转换 |
| `random_page_cost` | 1.1 | SSD |
| `timescaledb.telemetry_level` | `off`（按合规要求） | 自建环境通常不想外发遥测 |

安装后先跑 `timescaledb-tune`，再按上表覆盖。

### 7.4 对象设计：我们这些表怎么改造

原则：**时间序列大表转 hypertable，小维表/实验表保持普通表**。

| 表 | 处理 | 时间分区键 | 说明 |
|---|---|---|---|
| `power_observations` | 转 hypertable | `observed_at` | PK `(farm_id, observed_at)` 已含时间列，满足"唯一索引必须含分区列" |
| `nwp_forecasts` | 转 hypertable | `valid_at` | PK `(farm_id, valid_at)` 满足要求 |
| `nwp_revisions` | 转 hypertable | `valid_at` | PK `(farm_id, issued_at, valid_at)` 含时间列；`source` 可作 chunk skipping 列 |
| `predictions` | 转 hypertable | `valid_at` | PK `(run_id, origin, valid_at)` 含时间列 |
| `wind_farms` / `model_runs` / `dataset_splits` | **保持普通表** | — | 小表；hypertable 之间的外键官方不支持，必须留一侧为普通表 |

四条硬限制（官方文档，必须提前对齐）：
1. 分区列不能为 NULL。
2. 唯一索引必须包含所有分区列（我们的 PK 都满足）。
3. **不支持把行更新到另一个 chunk**（包括 upsert 改了分区列）。我们的 `upsert()` 主键含 `valid_at`，只要不改 `valid_at` 就没问题——这条要写进代码注释。
4. hypertable 之间不能建外键；我们的外键都指向普通表，满足。

分区策略：**只按时间分区，不加空间（farm_id 哈希）维度**。理由：10–100 个站的"站内查询"用普通索引 + 分区裁剪就够；加空间维度会强制所有唯一索引包含 `farm_id`（虽然我们已是如此），但会让 chunk 数乘以分区数，得不偿失。

DDL 草案（Alembic 迁移里写成幂等形式）：

```sql
CREATE EXTENSION IF NOT EXISTS timescaledb;

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

-- 冷数据转列存；阈值 > 最大修订延迟（先用 90 天）
ALTER TABLE nwp_revisions SET (timescaledb.compress = true);
SELECT add_columnstore_policy('nwp_revisions', INTERVAL '90 days');
```

`chunk_time_interval` 起步值按"活跃 chunk 索引 ≤ 内存 25%"复核后调整；功率表 30 天是因为每站每小时才一行，量小。

### 7.5 迁移步骤（从现有 PG 迁到 TimescaleDB 实例）

1. **冷备**：`pg_dump -Fc` 全量（迁移前唯一可靠的退路）。
2. **起新实例**：PG 18.6 + TimescaleDB 2.32.0，`CREATE EXTENSION timescaledb`。
3. **建结构**：Alembic 迁移到最新（先不加 hypertable 语句），再执行 7.4 的 `create_hypertable`。
4. **灌数据**：`pg_restore` 顺序为"普通表 → 大表"；大表灌完再做 `create_hypertable(migrate_data => true)`，比"先建 hypertable 再逐行灌"快一个量级。
5. **校验**：行数、`sum(power)`、`count(distinct farm_id)`、时间范围四项与源库逐表比对；再抽 100 条随机行比对内容。
6. **切应用**：改 `DATABASE_URL`，先只读验证，再切写入。
7. **观察一周**：作业历史、chunk 数、压缩率、查询 P95。

**回滚**：TimescaleDB 没有官方"撤销 hypertable 变回普通表"的路径，回滚只能靠第 1 步的 `pg_dump` 恢复，或改回旧实例。所以第 1 步不是可选项。

**逻辑复制不可用**：官方明确不推荐用逻辑复制同步 TimescaleDB，所以"边同步边切换"的平滑迁移在这里行不通，必须接受一次短暂停机（我们数据量小，停机窗口 10 分钟级）。

### 7.6 监控与告警（只列必须的）

| 指标 | 来源 | 阈值 |
|---|---|---|
| 后台作业失败 | `timescaledb_information.job_stats` | 任一 `last_run_status <> 'Success'` 立即告警 |
| 连续聚合滞后 | `job_stats.last_successful_finish` 与当前时间差 | > 2 个刷新周期告警 |
| chunk 数量 | `SELECT count(*) FROM timescaledb_information.chunks` | 单表 > 500 检查间隔 |
| 压缩率 | `hypertable_columnstore_stats` | 低于 70% 检查 `segmentby/orderby` |
| 复制延迟 | `pg_stat_replication` | > 30 秒告警 |
| 备份新鲜度 | pgBackRest info | 最近全量 > 26 小时告警 |

### 7.7 明确不做的事

- **不动本机 Mac 实例**：PG 18.7 不在 2.32.0 支持范围，本地继续用原生 PG 跑实验，生产另起。
- **不把数据库升级当优化节点**：模型精度不会因为换库改变，`docs/optimization-progress.json` 不新增节点。
- **不上分布式 hypertable**：官方已在 2.14 sunset，别回头。

---

## 八、对我们的收益排序（哪些确定、哪些只是可能）

**确定的收益**
1. 存储与备份体积降一个量级（实测 −90% 量级），三年数据不删也放得下。
2. 保留策略用 `DROP CHUNK`，删除瞬时、不膨胀、可审计。
3. 误差报表/看板走连续聚合，不再扫原始表。
4. `gapfill`/`locf`/`time_weight` 省掉一批手写窗口函数，缺测与时间加权有了标准做法。
5. 迁移成本接近零：应用代码、ORM、Alembic、备份工具全不动。

**可能的收益（未证实，别当承诺）**
6. 大范围聚合查询变快——我们目前没有这类负载，等有看板再测。
7. 插入吞吐提升——我们写入量小，不是瓶颈。

**不会改变的事**
8. 模型精度。库是管道，不是特征。

---

## 九、下一步（可执行，按顺序）

**P0｜真机验证（1 天）**：在目标 Linux 机器起 `timescale/timescaledb-ha:pg18`（对应 PG 18.6），把本地三张大表导进去，量三件事——迁移耗时、实际压缩率（`hypertable_columnstore_stats`）、5 条典型查询的 P50/P95。这一步把本文所有"官方口径"换成"我们的数字"。

**P1｜迁移脚本（半天）**：新增 Alembic 迁移 `0003_timescaledb`，含 `CREATE EXTENSION` + 四张 `create_hypertable`（幂等、可重复执行），并在 `docs/realtime-readiness.md` 补一节"生产库形态"。

**P2｜策略与报表（1 天）**：三张表的列存/保留策略 + 两张连续聚合（分站×提前量误差、日可用率），采集一周作业历史。

**P3｜演练回滚**：用 `pg_dump` 恢复一次，确认 RTO 数字真实。

**我需要你确认的两件事**（决定 P0 在哪跑）：
1. 目标环境：自有 Linux 服务器 / 云主机 / K8s？操作系统与内存规格？
2. 是否接受"生产起步用 PG 18.6 + TimescaleDB 2.32.0"（而不是追最新的 18.7）？

---

## 十、参考

- TimescaleDB 版本与兼容性：`timescale/timescaledb` release notes 2.32.0（2026-11-12，兼容 PG 18.6 / 17.11 / 16.15；升级顺序先扩展后 PG）
- hypertable 与 chunk 定容：官方 *Understand hypertables* / *Improve hypertable and query performance*（默认 7 天，活跃 chunk 索引 ≤ 内存 25%）
- 列存与压缩编码：官方 *Hypercore*、*Compression methods*（delta、delta-of-delta、simple-8b、RLE、XOR、dictionary；"最高约 90%"）
- 压缩数据修改限制：官方 *Modify data in hypercore*（大改先转行存）
- 连续聚合：官方 *Continuous aggregates*（增量刷新、real-time aggregation）
- 限制清单：官方 *Limitations*（分区列非空、唯一索引含分区列、跨 chunk 更新不支持、hypertable 间外键不支持）
- 版本与许可：官方 *Compare TimescaleDB editions*（Apache 2 版 vs TSL Community 版；自建自用免费、不可作为服务售卖）
- 安装与 HA：官方 *Install TimescaleDB on Linux / Kubernetes*、*About high availability*（流复制；不推荐逻辑复制）
- 本项目实测：三张表行数/体积/索引（`pg_stat_user_tables`）、Parquet/zstd 列存压缩代理测量（2026-11-16）

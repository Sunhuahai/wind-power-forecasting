---
title: 广西风电场生产数据库结构与数据契约
status: design-and-migration-committed-not-deployed
updated: 2026-11-21
week: 2026-W47
tags: [风电功率预测, 广西, PostgreSQL, TimescaleDB, 数据契约]
---

# 广西风电场生产数据库结构与数据契约

## 一页结论

**目标架构：PostgreSQL 18.6 + 兼容的 TimescaleDB 2.32.x。**关系表负责场站、容量、模型发布、报送回执和政策版本；hypertable 负责高频功率观测、NWP 版本和逐目标时刻预测。这是在事务一致性、时间序列查询、修订追溯、Python/SQLAlchemy 兼容性之间的工程选型，不代表任何规模下都是绝对最优。

2026-11-21 已将独立业务表定义写入 [Alembic 0003](../../alembic/versions/0003_guangxi_operational.py)，并提供 [TimescaleDB 激活脚本](guangxi-timescale-activation.sql)。**提交代码不等于在 Omarchy 或生产库完成迁移。**新表先作为标准 PostgreSQL 表；Linux 环境完成插件安装、版本预检与备份后，才可独立启用 hypertable。保留旧 `power_observations`、`nwp_forecasts`、`predictions` 与 2012/2013 基准实验，不从归一化小时数据反推真实 MW 容量。

## 1. 为什么这样分表？

| 表 | 作用 | 存储类型 | 唯一标识 |
|---|---|---|---|
| `wind_farms` | 场站身份，已有表 | 普通表 | `wind_farms.id` |
| `farm_capacity_history` | 容量及生效区间、依据 | 普通表 | 场站、容量生效时间 |
| `operational_power` | 实发、可用、理论功率及迟到修订 | hypertable 目标 | 场站、目标时刻、来源、接收时间 |
| `operational_nwp` | NWP 来源、起报/接收时间、气象值及修订 | hypertable 目标 | 场站、目标时刻、来源、起报、接收 |
| `operational_events` | 限电、故障、检修及豁免审批 | 普通表 | 事件 ID |
| `forecast_issuances` | 每次业务预测的固定发布记录 | 普通表 | 发布 ID，另限制序列内同一发布时间唯一 |
| `forecast_values` | 每次发布的 15 分钟功率曲线 | hypertable 目标 | 发布 ID、目标时刻 |
| `dispatch_submissions` | 发给调度的原始报文哈希和提交时间 | 普通表、追加记录 | 提交 ID |
| `dispatch_receipts` | 调度回执及接收时间 | 普通表、追加记录 | 回执 ID |
| `gx_policy_versions` | 已公布正式版与征求意见稿的规则元数据 | 普通表 | 规则版本 |
| `gx_daily_evaluations` | 逐场站日评分、完整性、使用版本 | 普通表、追加记录 | 评价 ID |

高频表的主键均包含时间分区列，适合 TimescaleDB；场站、发布、回执和评分保留关系型事务语义。**本项目不自动清除历史报送和考核证据。**

所有真实业务时间字段使用 PostgreSQL `TIMESTAMPTZ`，即绝对时刻；展示与业务日按 `Asia/Shanghai` 转换。来源不明的旧数据时间不推断为北京时间。建议将 15 分钟 `valid_at` 定义为**时段起点**；若调度采用时段终点，应在接入层显式转换并保存原始标签。

## 2. 以广西 100 MW 风电场为例

本节为**合成样例**，不是已获取的广西调度上传模板，也不是实际风场数据。内部交换可以用 CSV；正式报送须由场站调度端提供文件/API 规范。

场站 `GX001` 设为 `farm_id=101`，在现有 `wind_farms` 中同时填写必填的 `power_unit=MW` 与 `time_semantics=Asia/Shanghai/interval_start`；2026 年 11 月容量为 `100 MW`。在 `farm_capacity_history` 中记录该容量的生效时间、数值和来源；容量不是从公开数据中推导的。

**历史功率（进入 `operational_power`）：**

```csv
farm_id,valid_at,source,received_at,actual_mw,available_mw,theoretical_mw,curtailed
101,2026-11-21T08:00:00+08:00,scada,2026-11-21T08:02:00+08:00,40.0,40.0,42.0,false
101,2026-11-21T08:15:00+08:00,scada,2026-11-21T08:17:00+08:00,42.1,42.1,44.0,false
101,2026-11-21T08:30:00+08:00,scada,2026-11-21T08:32:00+08:00,41.3,41.3,43.0,false
```

其中，`actual_mw` 是真正发出的功率；`available_mw` 表示在对应风况和设备状态下本可发出的功率。限电可能使实际值显著低于可用值。应另外保存限电事件与审批，不能用受限发电量直接训练正常天气到可用功率的关系。

**天气预报（进入 `operational_nwp`）：**

```csv
farm_id,source,issued_at,received_at,valid_at,u100,v100,temperature_k
101,weather-vendor-a,2026-11-21T06:00:00+08:00,2026-11-21T07:30:00+08:00,2026-11-22T00:00:00+08:00,5.0,4.8,296.15
101,weather-vendor-a,2026-11-21T06:00:00+08:00,2026-11-21T07:30:00+08:00,2026-11-22T01:00:00+08:00,5.2,5.0,295.85
```

`u100/v100` 为 100 m 高度风矢量分量（m/s），`temperature_k` 为开尔文。NWP 可保持供应商原生小时级时间分辨率，再在特征构造中按固定、可回测的方法对齐至功率预测目标时刻。**不能插值未来实际功率来伪造 15 分钟真值。**

**一次预测发布（进入 `forecast_issuances`）：**

```csv
issuance_id,farm_id,product,series_id,model_version,issued_at,cutoff_at
gx001-da-20261121,101,day_ahead,gx001-da-v1,gx-model-v1,2026-11-21T08:45:00+08:00,2026-11-21T08:40:00+08:00
gx001-us-20261121-1200,101,ultra_short,gx001-us-v1,gx-model-v1,2026-11-21T12:00:00+08:00,2026-11-21T11:58:00+08:00
```

**每次发布的逐点结果（进入 `forecast_values`）：**

```csv
issuance_id,valid_at,lead_minutes,predicted_mw
gx001-da-20261121,2026-11-22T00:00:00+08:00,915,44.0
gx001-da-20261121,2026-11-22T00:15:00+08:00,930,45.0
gx001-us-20261121-1200,2026-11-21T12:15:00+08:00,15,47.6
gx001-us-20261121-1200,2026-11-21T12:30:00+08:00,30,48.1
```

样例只展示部分行。日前 D+1 需要次日 **96 点**，实际完整中期报送还涉及未来 240 小时共 960 点；超短期每 15 分钟发布一批，每批 **16 点**。12:15 再次发布的 16 点不能覆盖 12:00 发布的原批次。正式版超短期评分需要对同一目标时刻读取此前 16 批预测。

`issue_time/issued_at` 表示预测何时发布，`valid_time/valid_at` 表示预测哪一个运行时刻。前者决定输入可用性和考核版本，后者用于与实际功率对齐。`lead_minutes` 的单位是分钟，例中 11 月 21 日 08:45 到次日 00:00 为 915 分钟。

**调度报送与回执：**预测生成不等于被调度端接受。`dispatch_submissions` 保存报文对象路径、SHA-256 和实际提交时间；`dispatch_receipts` 追加调度返回的状态、时间及响应。场站真实文件格式、接口、回执码和上报截止时间必须与广西对应调度机构核对。

## 3. 两种运行流程

**D+1：**以 11 月 21 日 08:45 发布为例，预测输入截止到 08:40，选取当时已经发布且接收的 NWP、历史 SCADA 和容量快照，生成次日 00:00—23:45 的 96 个目标功率，并冻结发布 ID。真正发送给调度的曲线应包含规定的完整报送范围。这里的 D+1 不是每个点都提前固定 24 小时，次日首点与末点的提前量不同。

**超短期：**在 11 月 21 日 12:00，预测 12:15—16:00 的 16 点；12:15 再次预测 12:30—16:15。要评价目标时刻 16:00，需要 12:00、12:15……15:45 这 16 次发布中指向 16:00 的预测。

为了防止数据泄漏，NWP 必须满足 `issued_at <= cutoff_at` 且 `received_at <= cutoff_at`；历史功率还必须满足 `valid_at < 业务 issued_at` 和 `received_at <= cutoff_at`。仓库新增 [as-of 取数函数](../../src/windpower/db/operational_queries.py)。**未来真实训练样本也必须按历史发布时刻回放，不能直接拿事后修订的最新 NWP。**

## 4. 为什么政策影响评价目标？

模型首先要预测真实风电功率，以便电网提前安排发电、备用和交易。预测偏高时，可能需要其他电源或调节资源补足出力；预测偏低时，可能需要修改其他机组计划，特定情况下甚至可能增加弃风。具体结果取决于运行约束，单次误差不等于停电、弃风或同量现金损失。

正式公开细则中，广西日前 D+1 准确率门槛为 **60%**，超短期 **65%**，对应考核系数各为 **2**。若一座 100 MW 场站某项准确率为 58.4%，比日前门槛低 1.6 个百分点，按不足 1 个百分点向上取整为 2 档，条文原始折算是 `100 × 0.2 h × 2 × 2 = 80 MWh`。**80 MWh 是考核电量，不是实际少发电量，也不是最终账单。**月度归并、封顶和金额仍需调度侧对账。

指标使用逐时刻分母 `max(考核参考功率,0.2×当月装机容量)`。在容量 100 MW、实际功率 20 MW 的时刻，8 MW 误差按容量归一化是 8%，按政策公式则是 40%。最终日准确率还需聚合 96 点。因此模型评价应当同时展示 **MAE、RMSE、广西公开政策准确率、达标率、各提前量误差与完整性**，而不是只保留单一 RMSE。

## 5. 已提交的评分入口

- [政策纯函数评分器](../../src/windpower/evaluation/guangxi.py)：支持正式版 D+1/超短期，草案单列；完整 96 点才生成准确率。
- [数据库评价入口](../../src/windpower/evaluation/guangxi_db.py)：指定真实场站、业务日、模型序列/日前发行 ID 和观测版本截止时间。
- [合成单元测试](../../tests/test_guangxi_policy.py)：测试低功率分母、跨日、16 版均值、草案根号位置、缺报、限电参照功率等。
- [TimescaleDB 激活脚本](guangxi-timescale-activation.sql)：仅供空业务表、服务器预检备份后执行，尚未在生产部署。

在 Omarchy 上迁移并导入真实数据后，使用以下入口：

```bash
# 日前：显式选择当时冻结的唯一发行 ID
uv run windpower evaluate-guangxi --farm-id 101 --target-day 2026-11-22 \
  --product day_ahead --issuance-id gx001-da-20261121

# 超短期：指定连续序列和模型版本，拒绝混合重跑
uv run windpower evaluate-guangxi --farm-id 101 --target-day 2026-11-22 \
  --product ultra_short --model-version gx-model-v1 --series-id gx001-us-v1

# 草案口径仅用于前瞻分析，不能声称已正式生效
uv run windpower evaluate-guangxi --farm-id 101 --target-day 2026-11-22 \
  --product ultra_short --model-version gx-model-v1 --series-id gx001-us-v1 \
  --rule-version GX_WIND_DRAFT_202608
```

示例默认以 `--power-source scada` 读取 SCADA 数据，真正上线前需经场站确认权威参考功率数据源；不同来源的数据不会被自动混为同一序列。评分默认只读；显式 `--save` 才将研究评价结果追加存入 `gx_daily_evaluations`。缺少观测、预测版本或发生豁免而分母规则未确认时，评分返回 `incomplete` 与原因，**不输出貌似正式的准确率**。即使标记 `complete`，也只是完整条件下对公开公式的研究回放，不等于调度中心认证的正式结算成绩。

## 6. 部署前的验收条件

1. 在 Omarchy 检查 PostgreSQL、TimescaleDB 的真实兼容版本、备份和部署权限；执行 Alembic 0003 后核对新增表结构。空表的 hypertable 激活独立操作，不转换旧实验表。
2. 获取真实 15 分钟 SCADA（实发、可用功率、限电信息）、NWP 批次时间、容量历史和调度回执，审核字段/单位/时区与数据完整性。
3. 逐历史发布时间回放输入：不得使用预测截止后才收到的 NWP 与观测。
4. 用合成单测核验公式，再评价真实数据；保留 MAE/RMSE、每提前量指标及政策指标的两种版本。
5. 获取调度执行说明，核对豁免、缺报、96 点分母、月度归并与上传格式，在此之前不自动提交报文。

目前仅完成**数据库迁移与评分代码编写**，没有在 Omarchy 执行迁移、模型训练或真实调度考核。报告中的 CSV 是内部推荐契约，不是官方规定的报文格式。

## 参考

- [南方区域风电正式细则](https://nfj.nea.gov.cn/xwzx/tzgg/202502/P020250214585645425665.pdf)，第 22 条及广西附表。
- [2026 年部分条款征求意见稿](https://nfj.nea.gov.cn/xwzx/tzgg/202608/P020260824351987511048.pdf)，未核实后续正式实施。
- [TimescaleDB 2.32.0 发布记录](https://github.com/timescale/timescaledb/releases/tag/2.32.0)，兼容 PostgreSQL 至 18.6；真正部署时必须再次核对版本。
- [既有数据库选型调研](../周进展/2026-W46（11-09～11-15）/01_数据库选型调研.md)，保留历史依据与迁移限制。

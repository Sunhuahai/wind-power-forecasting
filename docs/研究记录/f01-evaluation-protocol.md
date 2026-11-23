---
title: F01 评价协议（预注册与测试集使用记录）
type: evaluation-protocol
status: active-preregistered-and-test-consumed
created: 2026-11-23
updated: 2026-11-23
week: 2026-W47
dataset_sha256: 1f81153d82242214201776d5cda6f302dc849d32f7f79f56cb3ec968f992b663
config: configs/f01_experiment.json
machine_readable: artifacts/f01/summary.json · artifacts/f01/test_summary.json · artifacts/f01/test_manifest.json
tags: [风电功率预测, F01, 严格评估协议, 预注册]
---

# F01 评价协议

> 本文把 F01 的数据划分、可见性假设、门禁条件和测试集使用状态固定下来。
> 参数以 `configs/f01_experiment.json` 为唯一事实来源；本文解释"为什么"，不重复每个字段。

## 一页结论

| 问题 | 结论 |
|---|---|
| 数据 | F01 单台风机，10 分钟 SCADA，2023-12-11 00:00 ～ 2024-12-11 23:50，52,749 行 |
| 划分 | 训练 2023-12-11～2024-09-01；验证 2024-09-01～2024-11-01；测试 2024-11-01～2024-12-12 |
| 划分依据 | 按目标时刻（`valid_at`）左闭右开；同一样本只属于一个段；约 8 / 2 / 2 个月 |
| 可见性假设 | `OFFLINE_REPLAY_COMPLETED_INTERVAL`：发布时刻 T 只能用 `observed_at ≤ T−10min` 的记录 |
| 验证门禁 | `F01_FIRST_ROUND_VALIDATION_GATE`，2026-11-21 通过 |
| 测试集状态 | **已于 2026-11-22 用冻结配置一次性评分**（`frozen_config_sha256=d8191cc2…`），此后不得再用于任何选择 |
| 广西政策准确率 | **不适用**，字段保持 `null`：单机 ≠ 场站并网点，10 分钟 ≠ 调度 15 分钟参考曲线 |

## 1. 为什么是 8 / 2 / 2

数据只有 367 天，要同时满足三件事：

1. 训练段覆盖完整季节，至少跨过冬季、春季、夏季；
2. 验证段足够长，能容纳按日/按周的配对比较（首轮验证段 7,178 个发布、171,720 个有效样本）；
3. 测试段留出约两个月，包含一次明显的季节转换，且不与验证段重叠。

按目标时刻切分而不是按发布时刻切分，是因为一条发布可以指向跨段的多个目标。
若按发布时刻切，会让同一目标同时出现在两个段里，造成标签泄漏。

## 2. 可见性假设及其边界

离线数据没有真实接收时间，因此协议显式声明一个假设：

- `统计时间 = t` 的记录视为区间 `[t, t+10min)` 的平均，在 `t+10min` 才完整可见；
- 发布时刻 `T` 只允许使用 `observed_at ≤ T − 10min` 的记录；
- 该假设在"区间起点"和"区间终点"两种语义下都不会引入未来信息。

**这不是生产实时能力的证明。** 真实上线必须用采集端的接收时间，并按 as-of 规则截断。

## 3. 门禁：打开测试集之前必须满足

`F01_FIRST_ROUND_VALIDATION_GATE`（已在 2026-11-21 通过）：

| 条件 | 要求 |
|---|---|
| c1 | LightGBM 验证期 pooled RMSE < Persistence 验证期 pooled RMSE（同一样本集） |
| c2 | LightGBM 各提前量 RMSE 的均值 < Persistence 各提前量 RMSE 的均值 |
| c3 | `tests/test_scada_samples.py` 与 `tests/test_scada_quality.py` 全部通过（防泄漏与协议） |

首轮验证结果与门禁细节见 [F01 首轮基线](f01-baseline-first-round.md)。

## 4. 测试集使用记录（重要）

| 项 | 值 |
|---|---|
| 状态 | `opened_one_time_final_scoring` |
| 授权 | 用户授权，2026-11-22 |
| 冻结配置 SHA-256 | `d8191cc22a733b6456cf6ef69b2828a4ecec9a2e4d7188176a31c16379ea58a2` |
| 快照 ID | `df3c1f37-7459-44ec-a39e-672834e5b836` |
| 代码 commit | `990c38774e3a0d62401b5de4ce5766b503a9dea1` |
| 结果 | `artifacts/f01/test_summary.json`、`test_per_lead.csv`、`test_per_day.csv`、`test_report.md` |

**约束**：

1. 测试结果只能用于报告，不能反过来指导模型选择、特征增删、阈值调整或数据清洗；
2. 同一测试段不得以"改进模型"为理由重复评分；
3. 若必须验证新想法，只能在验证段做；要得到新的无偏测试估计，需要等新数据或新的时间前推段，并重新预注册。

## 5. 广西政策口径

F01 是单机、10 分钟、无场站并网点计量、无可用功率/限电记录、无 NWP 批次、无报送回执，
因此：

- `policy_eligibility.eligible = false`；
- `GX_policy_accuracy = null`，原因 `single_turbine_10m_scada_is_not_farm_dispatch_reference`；
- 任何基于 F01 的"政策准确率"若被计算出来，只能标为**研究性代理**，不能称为正式考核成绩。

正式政策评价需要：多机组与场站并网点总功率、15 分钟参考功率、容量档案、NWP 起报/接收时间、
调度报送与回执。见 [广西生产库与数据契约](../部署与工程/guangxi-operational-storage.md)。

## 6. 来源口径确认（2026-11-23）

| 项 | 记录 |
|---|---|
| 资产归属 | 广西某风场 F01 风机（依据项目负责人确认） |
| 时区 | `Asia/Shanghai`（依据项目负责人确认） |
| 区间标签 | 10 分钟区间**起点**（依据项目负责人确认） |
| 书面依据 | 尚无设备导出规范；上述为口头确认，待书面材料归档后升级为正式契约 |
| 机器可读 | `configs/f01_data_semantics.json`（独立文件，不改动已冻结的实验配置） |
| 对存储的影响 | **无转换**：数据库仍保存原始 naive 时间戳，审计报告与既有产物不变 |
| 对评价的影响 | 不改变 `policy_eligibility = false`；单机数据仍不能作为场站考核参考 |

> `configs/f01_experiment.json` 的 SHA-256 已写入测试集快照（`frozen_config_sha256`），因此语义确认单独成文，
> 不回写实验配置。若后续设备导出规范与上述确认不一致，应新开版本记录差异，不得静默改写历史产物。

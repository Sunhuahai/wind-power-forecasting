# 文档索引

本项目的文档分三类：**当前口径**（用来判断现状与继续工作）、**历史记录**（已完成的实验报告）、**归档**（被否路线与边界说明）。

## 当前口径（优先读这些）

| 文档 | 内容 |
|---|---|
| [project-summary.md](project-summary.md) | **一页读懂**：从零到现在做了什么、9 个节点、走过的弯路、定下的规则、实时预测准备 |
| [optimization-progress.md](optimization-progress.md) | 优化进展：9 个成功节点、进展图、每个有效方法的具体做法与区间；当前最优 **0.156009848** |
| [optimization-progress.json](optimization-progress.json) | 进展注册表（机器可读，进展图与表格的唯一数据源） |
| [optimization-saturation.md](optimization-saturation.md) | 饱和评估：最近 5 轮效应量、余量测算、已封路线清单、继续涨分的先决条件 |
| [optimization-tricks.md](optimization-tricks.md) | 逐轮记录：有效方法、被否方案（含门禁数字）、两条边界说明（OOF 分布、越界记录） |
| [session-summary-2026-11-14.md](session-summary-2026-11-14.md) | 本轮总结：现状数字、时间线、被否路线、可复用经验、协议红线、下一步所需新数据 |
| [realtime-readiness.md](realtime-readiness.md) | 实时预测准备：差距分析、接口契约、分阶段上线计划、已知风险 |
| [database-selection.md](database-selection.md) | 数据库选型调研：候选对比与加权适配度、PostgreSQL 生产部署方案 |
| [timescaledb-evaluation.md](timescaledb-evaluation.md) | TimescaleDB 深度评估：相对其他关系型库的逐项优势、实测压缩比、许可证、生产部署与迁移方案（生产主库定为此形态；[迁移草案](timescaledb-migration-draft.sql) 备而不发） |
| [figures/](figures/) | 进展图（png/pdf/svg/csv）与原始实验图 |

## 历史实验报告

| 文档 | 内容 |
|---|---|
| [experiment-report.md](experiment-report.md) | 首次全流程验收：数据、清洗、训练、预测、指标 |
| [paper-report.md](paper-report.md) | 图表化完整报告：数据字段、特征、训练样例、十站结果图 |
| [kpower-reproduction.md](kpower-reproduction.md) | kPower 复现：两层 OOF 结构与方法说明 |
| [kpower-reproduction-results.md](kpower-reproduction-results.md) | 复现结果：十站逐站数字与远端起因 |
| [physics-experiment.md](physics-experiment.md) | 物理融合实验：残差学习改善 MAE、未改善 RMSE |
| [architecture.md](architecture.md) | 代码架构与模块职责 |

## 每轮实验证据

`artifacts/kpower_ablation/ablation_20261114_v1/<experiment>/` 下每个实验目录都带 `protocol.json`（训练 / 早停 / 权重 / 评分各用了哪些数据）、`metrics.csv`、`paired.json`、`decision.json`。模型文件与逐小时预测不进版本库。

## 约定

- 数据划分固定：训练 2012，验证 2013 上半年（只做选择/早停/权重），测试 2013 下半年（只评分）。
- 只有 RMSE 前进且七日块配对区间上界 < 0 才登记；开测试集还需验证集区间上界 ≤ −0.0002。
- 失败方案只进 tricks，不进进展图。

## Obsidian 库的组织方式

个人库 `01_Projects/风电功率预测/` 按**周**组织：

- `00_项目索引.md`：库的总入口，含当前状态与导航。
- `周进展/2026-Wxx（MM-DD～MM-DD）/`：当周所有文档，固定含 `00_本周总览.md`（一页读完）+ 分主题文档 + `附件/`。
- `历史资料/`：基线报告、物理实验、学习笔记、`旧版本/`（被取代的文档不删除）。

每轮结束时把总结写进**当周文件夹**，仓库 `docs/` 里保留同名可版本化副本。

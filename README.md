# 风电功率预测（Wind Power Forecasting）

风电功率预测工程仓库，用于**时序数据库部署**与**预测模型开发**。当前基于公开数据集 GEFCom2014-Wind（10 个风电场）完成从数据入库、特征构造、模型训练到逐日 24 小时滚动预测与评估的全流程。

## 目录结构

| 目录 | 说明 |
|---|---|
| `src/windpower/` | 核心包：数据导入清洗（`data/`）、特征工程（`features/`）、模型训练（`models/`）、评估与出图（`evaluation/`）、数据库访问（`db/`）、推理打包（`serving/`） |
| `alembic/` | 数据库表结构迁移脚本 |
| `configs/` | 实验与场站配置（`experiment.json`、`sites.json` 等） |
| `scripts/` | 实验迭代脚本与报告/图表生成工具，见 [scripts/README.md](scripts/README.md) |
| `tests/` | 防泄漏、时间边界、数据质量与数据库测试 |
| `docs/` | 项目文档与周进展，见 [docs/README.md](docs/README.md) |

## 快速开始

环境要求：Python 3.13、`uv`、PostgreSQL 18.6（或 Docker Compose；macOS 运行 LightGBM 需 `brew install libomp`）。

```sh
# 1. 配置环境与依赖
cp .env.example .env
uv sync --extra dev

# 2. 启动并初始化数据库（默认 127.0.0.1:55432）
uv run windpower db-start
uv run windpower db-init

# 3. 跑通单站全流程（下载 -> 入库 -> 训练 -> 预测 -> 评估）
uv run windpower run-all --site 1

# 4. 运行测试
uv run pytest
WINDPOWER_TEST_DB=1 uv run pytest   # 含真实数据库测试
```

常用子命令：`db-health`、`download`、`import-data`、`quality`、`build-samples`、`train`、`predict`、`evaluate`、`forecast`、`run-sites`。

## 数据库部署

- 所有正式训练、验证与预测样本均从 PostgreSQL 读取。
- 核心表：`wind_farms`（场站）、`power_observations`（功率观测）、`nwp_forecasts`（气象预报）、`dataset_splits`（划分定义）、`model_runs`（训练记录）、`predictions`（预测结果）。
- 数据库选型、生产部署架构与多批次 NWP 迁移方案见 [数据库选型调研](docs/周进展/2026-W46（11-09～11-15）/01_数据库选型调研.md) 与 [代码架构](docs/部署与工程/architecture.md)。

## 模型开发

- **严格评估协议**：`2012` 年训练，`2013` 上半年验证（仅用于模型选择、早停与融合权重），`2013` 下半年测试（仅做最终评分，不参与调参）。
- **当前基线与进展**：公开数据集 10 站严格协议最优 RMSE 为 **0.156010**（相对原 C 基线降低 **8.47%**），详见 [优化进展](docs/研究记录/optimization-progress.md)。

## 广西真实风电场部署目标（2026-W47）

现有 GEFCom2014-Wind 小时级公开数据继续用于算法研究，原 MAE/RMSE 与严格 2012/2013 训练验证测试划分不变。真实广西风电场将采用 **PostgreSQL + TimescaleDB** 的独立 15 分钟生产表：保留 SCADA 实发/可用功率、NWP 发布与接收时间、模型发布批次、预测目标时间、限电事件、调度报送回执和政策评分。详情见 [生产数据库结构与业务示例](docs/部署与工程/guangxi-operational-storage.md) 和 [本周政策报告](docs/周进展/2026-W47（11-16～11-22）/01_广西风电功率预测考核政策与训练评估适配.md)。

后续真实场站评价将同时输出 MAE、RMSE、广西日前 D+1/超短期公开公式准确率、完整性和提前量诊断；独立命令为 `windpower evaluate-guangxi`。本次仅提交迁移及评分代码，**尚未在 Omarchy 执行迁移、测试或真实数据评价**，不将小时级公开数据的 RMSE 伪装为正式广西考核成绩。

## 文档导航

- 全部文档索引：[docs/README.md](docs/README.md)
- 数据库与部署：[数据库选型调研](docs/周进展/2026-W46（11-09～11-15）/01_数据库选型调研.md) · [实时预测准备](docs/部署与工程/realtime-readiness.md)
- 模型与实验：[实验报告](docs/研究记录/experiment-report.md) · [图表报告](docs/研究记录/paper-report.md) · [优化技巧与失败记录](docs/研究记录/optimization-tricks.md)

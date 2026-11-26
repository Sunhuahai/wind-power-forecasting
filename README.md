# 风电功率预测

当前主线使用 F01 单台风机的 10 分钟 SCADA，完成原始数据审计、版本化入库、短期滚动预测和验证评价。GEFCom2014 公开数据实验已归档，历史 CLI 命令仍可用。[GEFCom2014 归档](docs/归档/gefcom2014/README.md)

## 快速开始

在项目根目录运行，需要 Python 3.13 和 `uv`。审计直接读取私有 CSV。入库和基线需要已迁移的 PostgreSQL 18.6 开发库。

```sh
uv sync --extra dev

# 默认读取 data/raw/f01/F01号风机十分钟数据.csv。
# 文件放在别处时，指定私有路径。
# export F01_CSV_PATH=/path/to/private/F01.csv

# CLI 默认使用此目录存放本轮产物，可用 --out-dir 指定其他目录。
F01_OUT=artifacts/f01-validation
uv run windpower audit-f01 --out-dir "$F01_OUT"
```

审计核对注册文件的 SHA-256、编码、行列数和时间网格，输出 `quality_report.json`。原始 CSV 仅保留在私有位置。网侧负功率保留原值，数据库保存源时间，避免替换原始时间戳。

接下来确认 `DATABASE_URL` 指向项目的 **开发库**。如需从空的本地开发库开始，可用 `uv run windpower db-start` 启动 Compose 服务，再用 `uv run windpower db-init` 初始化。[Omarchy 开发环境](docs/部署与工程/omarchy-setup.md)

```sh
uv run windpower import-f01 --out-dir "$F01_OUT"
uv run windpower baseline-f01 --out-dir "$F01_OUT"
uv run windpower final-test-f01 --split validation --out-dir "$F01_OUT"
```

- `import-f01` 按文件哈希版本化入库，重复导入幂等。用同一目录时，本轮质量报告会补充入库核对结果。
- `baseline-f01` 运行持续法、季节法、Ridge 和 LightGBM，生成验证报告、分提前量和逐日指标、模型文件及快照清单。运行会在开发库登记特征快照。
- `final-test-f01 --split validation` 读取该目录保存的模型和训练阈值，输出验证 MAE/RMSE。验证回放可以用于排查门禁未通过的运行。
- 基线与验证回放只查询注册文件版本中早于验证截止的观测，且仅保留提前量标签齐全的发布。验证末尾跨入测试期的发布会被排除，样本数可能与首轮历史报告不同。
- 首轮测试已于 2026-11-22 完成一次性评分，证据保存在 `artifacts/f01/`。日常复跑使用独立目录，模型选型和调参使用验证集。[F01 评价协议](docs/研究记录/f01-evaluation-protocol.md)

```sh
uv run pytest -q
```

默认测试使用合成 SCADA，数据库和真实 CSV 用例默认跳过。

## 项目导航

| 路径 | 用途 |
|---|---|
| [src/windpower/](src/windpower/) | F01 数据导入、样本构造、模型、评价和数据库访问 |
| [configs/f01_experiment.json](configs/f01_experiment.json) | 预注册的数据版本、训练与验证划分、可见性假设和门禁 |
| [scripts/README.md](scripts/README.md) | 实验、报告和仪表盘脚本 |
| [docs/README.md](docs/README.md) | 文档索引 |

- F01 实测与设计见 [数据审计](docs/研究记录/f01-data-audit.md)、[首轮基线](docs/研究记录/f01-baseline-first-round.md)和[真实数据架构](docs/周进展/2026-W47（11-16～11-22）/03_真实数据项目架构总览.md)。
- 广西现场部署采用 PostgreSQL + TimescaleDB，按历史发布和接收时间回放。正式评分需要 15 分钟场站参考功率、装机容量、历史预测发布及豁免等数据。F01 单机评价中的 `GX_policy_accuracy` 为 `null`。[广西生产库与数据契约](docs/部署与工程/guangxi-operational-storage.md)

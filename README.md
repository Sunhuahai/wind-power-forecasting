# 风电功率预测：GEFCom2014-Wind

从原始官方 Task 15 数据到 PostgreSQL、逐日 24 小时预测、独立测试和中文实验报告的 Python 项目。没有依赖其他风电预测仓库，没有使用 SQLite。所有正式训练、验证和预测样本都从 PostgreSQL 查询生成；Parquet 只是检查用的派生导出，不作为训练入口。

## 实验结果

[持续优化进展图与有效技巧](docs/optimization-progress.md)（含[饱和评估](docs/optimization-saturation.md)）：只展示确认改善的节点，当前十站 RMSE 为 **0.156010**，相对原 C 基线降低 **8.47%**。全部节点采用严格协议：只用 2012 年训练、验证集只做选择与早停；三个曾把 2013 上半年标签并入训练的最终重拟合节点已作废并从图中移除。

[图表完整实验报告](docs/paper-report.md) 直接展示数据字段、训练特征、真实训练样例及十站结果图。仓库包含汇总指标和图表；原始数据、模型权重和逐小时预测 CSV 在本地运行后生成。

## 环境与运行

需要 Python 3.13、uv、Docker Compose。macOS 另需 `brew install libomp`（LightGBM 运行库）。

```sh
cp .env.example .env
uv sync --extra dev
uv run windpower db-start
uv run windpower run-all --site 1
```

默认数据库只监听 `127.0.0.1:55432`，镜像严格为 `postgres:18.6`，持久卷挂载 `/var/lib/postgresql`。`.env` 的本地示例密码仅用于本机实验。`db-init` 会校验数据库版本为 18.6，再运行 Alembic；数据库服务应先启动。命令在项目根目录执行。

```sh
uv run windpower db-health
uv run windpower db-init
uv run windpower download
uv run windpower import-data --site 1
uv run windpower quality --site 1
uv run windpower build-samples --site 1
uv run windpower train --site 1
uv run windpower predict --site 1
uv run windpower evaluate --site 1
uv run windpower forecast --site 1 --origin 2013-12-31T00:00:00
uv run windpower run-sites
uv run pytest
WINDPOWER_TEST_DB=1 uv run pytest
```

本机项目位于 iCloud 目录，实际运行发现它会将 `_editable_impl_*.pth` 自动标记隐藏，Python 3.13 因而忽略 editable 包路径。当前 `.venv` 链接到 `~/.local/share/windpower-venv`（家目录，不在 iCloud 与临时目录内，不会被系统清理），普通 `uv run` 可直接使用。若换机器或该目录被删，重建一次即可：

```sh
export UV_PROJECT_ENVIRONMENT="$HOME/.local/share/windpower-venv"
uv sync --extra dev
rm -f .venv && ln -s "$UV_PROJECT_ENVIRONMENT" .venv
```

不要放进 `/private/tmp`：macOS 会定期清理临时目录，软链悬空后 `uv run` 会报 `File exists`。普通非同步目录不需要此操作。

`run-sites` 先完成 Zone 1，再依次完成 2–10，输出跨站指标。`predict` 是固定独立测试集的逐日回放；`forecast` 为单一起点的 24 小时预测，未来标签可缺失，但必须已有完整 NWP 和过去 24 小时功率。离线数据只能预测其覆盖的有效时间，不能凭空生成当前日期的 NWP。

原始 ZIP/CSV 留在 `data/raw/`；每站的模型、样本检查导出、清洗报告、CSV 和 PNG 位于 `artifacts/site_N/`。`report.md` 是每次真实运行生成的中文报告。每次训练生成新 UUID，保留旧实验；重复导入对站点/时间主键 upsert，重复预测对运行/起点/目标主键 upsert。`latest_runs.json` 指向最近完整训练的五组模型。

## 数据、时间和划分

唯一数据集为 [IEA Wind 列出的 GEFCom2014](https://iea-wind.org/task51/task51-information-portal/benchmarks/)，下载 [官方 ZIP](https://www.dropbox.com/s/pqenrr2mcvl0hk9/GEFCom2014.zip?dl=1)。解析内层 `GEFCom2014-W_V2.zip` 的 `Wind/Task 15/Task15_W_Zone1_10.zip` 和 `TaskExpVars15_W_Zone1_10.zip`，以及 `Wind/Solution to Task 15/solution15_W.csv`。官方说明原文保留为 `data/raw/Instructions.txt`。本次完整包可下载，无需使用 [Strathclyde 单站备份](https://pureportal.strath.ac.uk/en/datasets/supplementary-material-for-evaluation-of-wind-power-forecasts-an-/)。本项目没有复制该备份网站列出的任何实验代码。

时间原文如 `20120101 1:00`，解析为无时区时间；源时区未核实，不自动转换 UTC。功率是相对额定容量的 0–1 数值，不推测 MW。U10/V10/U100/V100 是预报风矢量，不是实测天气。

源文件没有发布和实际到达时间，`issued_at=NULL`。以源时钟每日 00:00 为起点、下一日界之前 1–24 小时 NWP 在该起点可用，是明确记录的竞赛回放假设，并非核实过的生产可用性。00:00 的目标属于前一天起点的第 24 小时。历史功率严格 `< origin`，所以 lag1 是前一小时；不会读取起点或未来功率。

配置见 `configs/experiment.json`。目标采用 `(start,end]`：训练 2012-01-01 至 2013-01-01，验证至 2013-07-01，测试至 2014-01-01；标签不跨边界。C 特征要求起点前 24 小时完整，所有模型使用同样完整的 24 小时预测日，以保证公平消融。验证期/测试期过去已发生的功率可用于之后的逐日起点，不用于重新拟合权重。2013 年 12 月答案只在测试评分及之后的合法历史中使用。

## 特征与模型

- A：U10、V10、U100、V100。
- B：A + 两高度风速、方向 sin/cos、100m 风速三次方、对数风切变、目标小时/年周期和 1–24 小时提前量。风向采用矢量指向编码，静风时方向置零。
- C：B + 起点前 1/2/24 小时功率、过去 24 小时均值/标准差/极值、23 小时端点变化量。

Persistence 复制最后一个可用小时功率。Ridge 使用 C、训练集 StandardScaler，alpha 由验证集选择。LightGBM 分别使用 A/B/C，验证集早停，固定随机种子。全部输出裁剪 [0,1]。不使用测试集调参，不用未来实测气象，不填造缺失值。缺失/非有限数据标记为 NULL；边界异常功率标记后置 NULL；重复源键拒绝导入；缺小时在报告中记录，相应不完整日排除。

MAE/RMSE 在逐小时样本上计算。因为功率已容量归一化，nMAE/nRMSE 与 MAE/RMSE 数值相同，乘 100 即额定容量百分比。同时输出每个提前量、每站、各特征组指标；跨站汇总按实际小时样本数加权 MAE、并由加权平方误差开根计算 RMSE。不使用分位数竞赛评分代替点预测评分。

## 数据库

|表|主键/用途|
|---|---|
|wind_farms|场站 ID；归一化单位和时间语义|
|power_observations|场站 + observed_at；原始时间、功率和质量标记|
|nwp_forecasts|场站 + valid_at；四分量、假设起点、未知 issued_at、可用性说明|
|dataset_splits|固定划分名；起止和边界规则|
|model_runs|UUID；场站、模型、特征组、配置、训练范围、权重路径和指标|
|predictions|运行 + origin + valid_at；lead、预测和实际标签|

观测、NWP、预测有效时间都有索引，观测和预测有边界约束。当前数据集每个有效时间仅一组 NWP，因此唯一键不含真实发布批次；接入企业多批次 NWP 时必须迁移为 `(farm,issued_at,valid_at)` 并做真实到达时间过滤，不能沿用本次可用性假设。

```sql
SELECT id, farm_id, model, feature_group, metrics FROM model_runs;
SELECT r.farm_id, r.model, r.feature_group, p.origin, p.valid_at, p.lead, p.prediction
FROM predictions p JOIN model_runs r ON r.id=p.run_id
ORDER BY p.valid_at LIMIT 24;
```

## 目录

`src/windpower/data/` 下载、解析、清洗和导入；`features/` 统一构造；`models/` 训练；`evaluation/` 指标和图表；`db/` SQLAlchemy PostgreSQL 数据访问；`alembic/` 迁移；`configs/` 实验配置；`tests/` 防泄漏、边界、质量和可选真实数据库检查；`docs/` 中文实验记录。

## 实际执行范围

请阅读 [实际验收与实验报告](docs/experiment-report.md)。Docker 在本机未安装，不能声称 Compose 路径已实际验证。本次以本机同版本 PostgreSQL 18.6 完成实验，具体完成项及真实数值见报告。原始数据和模型体积较大，默认不纳入版本控制。

## 物理融合实验

新增 [物理融合实验说明](docs/physics-experiment.md) 与独立 `physics-experiment` 命令，比较冻结 C、三参数场站等效物理曲线、物理残差学习和物理预测特征注入。先 Zone 1，再以相同实验标识续跑十站，不覆盖旧模型或报告。

```sh
uv run windpower physics-experiment --site 1 --experiment-id physics_20261109_v1
uv run windpower physics-experiment --all-sites --experiment-id physics_20261109_v1
```

[本次完整实验报告及 44 张图](artifacts/physics_experiments/physics_20261109_v1/report.md)：残差学习改善 MAE（0.123454 → 0.121856），未改善 RMSE（0.170454 → 0.170535）；配对 RMSE 差的 95% 区间跨零，不能宣称全面超越旧 C。

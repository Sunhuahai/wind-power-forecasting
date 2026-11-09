# GEFCom2014 风电冠军 kPower 方法重现实验（第一轮）

## 研究背景与复刻边界

比赛 **GEFCom2014 Wind** 的总冠军是 kPower（Landry、Erlinger、Patschke、Varrichio）。论文：

- Landry et al., *Probabilistic gradient boosting machines for GEFCom2014 wind forecasting*, International Journal of Forecasting 32(3), 1061–1066 (2016).
  DOI: https://doi.org/10.1016/j.ijforecast.2016.02.002
- Hong et al., *Probabilistic energy forecasting: Global Energy Forecasting Competition 2014 and beyond*, International Journal of Forecasting 32(3), 896–913 (2016).
  PDF: https://robjhyndman.com/papers/gefcom2014.pdf（表 3、表 9）。

已核实的核心结构：每站/分位数独立训练梯度提升机；主要 NWP 特征包括 10m、100m 风速与风向、100m 风能的前后相邻预测时刻、时间特征；利用其他风电场的预测信息做第二层建模；比赛以 99 个分位数 Pinball Loss 评分。

**本目录是论文方法的 Python/LightGBM 重实现，不是冠军源代码，也不是比赛原始成绩的逐数字复现。** 未找到可核实的 kPower 官方代码仓库或全部超参数；不要把本文的 3 分位数分数当成官方 99 分位数竞赛评分。

## 当前实际已完成的基线（10 站、42,432 小时）

|模型|MAE|RMSE|
|---|---:|---:|
|LightGBM C|0.123454|0.170454|
|纯等效物理功率曲线|0.136466|0.189000|
|物理曲线+LightGBM 残差|0.121856|0.170535|
|C+物理预测特征|0.122734|0.170425|

物理残差方案 MAE 有改善，但 RMSE 未改善；直接注入方案的 RMSE 优势非常小，置信区间覆盖零。上述是真实已有结果，与本次新冠军重现实验无关。

## 新实验算法

1. **NWP 邻域工程**：为每个预测起点构造 24 个未来小时的 NWP 特征。加入 100m 风速三次方在同一次 24 小时预报内的 -3/-1/+1/+3 小时邻域、3/5 小时局部均值和相邻梯度。边界截在当前预报日，不读取下一次起点的 NWP；这不是未来实测功率。
2. **第一层**：每个场站独立训练 q50 LightGBM。对第二层训练数据，采用按时间先后扩展的训练窗口生成 OOF（Out-of-Fold）预测：最初 90 天仅用于拟合，从第 91 天起每 30 天形成一个验证块。该预测块的任何标签均不参与对应第一层模型拟合或早停。
3. **第二层**：把 10 个场站第一层 q50 预测作为同一目标时刻的特征，再结合目标站点自身 C 特征及 NWP 邻域特征，训练点预测（L2）和 q10/q50/q90 分位数模型；绝不把其他场站在目标时刻的真实功率作为输入。
4. **验证与评分**：固定使用原项目训练 2012 年、验证 2013 年上半年、测试 2013 年下半年。第二层训练仅用诚实 OOF 时段。所有输出裁剪到 [0,1]，分位数用单调投影消除 crossing。输出同一样本 MAE、RMSE、三分位平均 Pinball 和 80% 区间覆盖率。
5. **存储**：正式标签与 NWP 均取自 PostgreSQL；独立输出模型 joblib、预测 CSV、场站报告；新点预测结果同时写入数据库 model_runs/predictions，保留原有 latest_runs.json 和所有旧实验。

请注意：第一层 NWP 全站预测在任何时候只依赖天气预报；如果邻站历史功率缺失，也不会因为该邻站的历史功率缺失而阻断第二层推理。目标站点本身的 C 特征仍要求预测起点前 24 小时观测完整，因此测试集范围与原 LightGBM C 一致。

## 执行

先启动原 PostgreSQL 18.6，并确认已有十站导入和原 LightGBM C 的权重/预测文件。

```bash
# 首先单站验证：第一层仍会读取十站天气并生成无泄漏 OOF
uv run python -m windpower.models.kpower_reproduction --sites 1

# 通过验收后一次性完成十站。请使用新的实验 ID。
uv run python -m windpower.models.kpower_reproduction --sites all --experiment-id kpower_v1_ten_sites

# 离线单元测试，无需数据库
uv run pytest tests/test_kpower_reproduction.py
```

输出：`artifacts/kpower_reproduction/<experiment-id>/site_N/predictions.csv`、各站 joblib、`site_metrics.csv`、`summary.json`。新实验拒绝覆盖同名目录。

**当前提交只完成算法实现与离线单元测试文件。尚未在用户本机 PostgreSQL/GEFCom2014 数据上真正运行；没有新的冠军模型 RMSE。** 若本轮明显优于原基线，再考虑增加 1%–99% 全部 99 个分位数、按比赛每月扩展训练集重训，以及更贴近论文的特定参数设置，专门比较 Pinball Loss。禁止据测试结果修改特征、树参数而反复试探。

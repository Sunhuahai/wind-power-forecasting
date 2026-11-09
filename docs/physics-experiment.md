# 场站等效物理模型与残差融合实验

新命令复用原 PostgreSQL、C 特征构造与固定时间划分。先完成原 `run-all` 或 `run-sites`，保留其 `latest_runs.json`、joblib 和逐小时预测 CSV；新实验会加载冻结的 C 并核对同样本，不用重新训练后的模型替代旧基线。

```sh
# 数据库应已启动；首次运行使用独立标识。
uv run windpower physics-experiment --site 1 --experiment-id physics_20261109_v1
# Zone 1 检查后，用同一标识继续其余站点，已完成站点不重训。
uv run windpower physics-experiment --all-sites --experiment-id physics_20261109_v1
# 新实验请换新标识，省略时自动生成时间戳+随机标识。
uv run windpower physics-experiment --site 1
WINDPOWER_TEST_DB=1 uv run --extra dev pytest
```

结果目录：`artifacts/physics_experiments/<experiment-id>/`。`manifest.json` 固定完整配置，修改配置后不能续跑同一标识。遇到中途失败的未完成站点目录会拒绝覆盖，应保留诊断并使用新实验标识重跑，避免混淆部分输出。

固定物理协议见 `configs/physics-experiment.json`：切入 [0,6]、额定 [6.5,20]、网格步长 0.25、平台 [0.3,1]；只用训练 MSE 拟合。没有切出参数，不能当作厂家风机模型。残差 LightGBM 使用完全相同 C 和超参数，验证残差早停。另比较 C 加物理预测一维的直接注入。训练函数不接受测试数据。

不确定性为 2000 次七日连续日历块 bootstrap；同日所有场站一起抽样，比较完全相同预测样本的误差差。报告中同时给 MAE/RMSE 差值和 95% 区间，保留负面结果。这里的区间不包括重新训练、其他年份或新场站的不确定性。

每站保存参数、模型、宽表逐小时预测、MAE/RMSE、逐时效和逐 NWP 风速区间误差、配对区间、资源统计及四张图。模型配置和新预测另写原数据库 `model_runs` / `predictions`，不修改旧行。冻结基线原始历史训练时间未知，成本比较用同配置受控重训，报告明确区分；重训不替换基线。

[本次完整图表报告](../artifacts/physics_experiments/physics_20261109_v1/report.md) 已包括全部 44 张结果图。最新结果为：残差融合 MAE 0.121856、RMSE 0.170535；原 C 为 0.123454 / 0.170454。MAE 改善，RMSE 没有可靠改善。直接注入为 0.122734 / 0.170425，微小 RMSE 改善的区间也跨零。

GitHub 保留参数、指标、图表与报告；joblib 和逐小时宽表 CSV 默认忽略，保留于实验本地目录。Obsidian 的新报告及独立附件目录保留这些结果数据，旧报告不改动。

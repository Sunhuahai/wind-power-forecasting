# scripts 目录索引

一次性实验脚本按"一轮一个文件"的方式冻结保存：**每个脚本就是当时跑出那个数字的原始代码**，因此刻意不做公共抽取，重复的样板代码是保持可追溯性的代价，不要为了"整洁"去重构它们（重构会让历史数字失去可复现的对应物）。

## 主流水线与工具

| 脚本 | 用途 |
|---|---|
| `plot_optimization_progress.py` | 由注册表重画进展图（png/pdf/svg/csv）与同步表格；`--add-result` 是登记入口 |
| `build_paper_report.py` | 生成图表化实验报告 |
| `build_database_deck.py` | 生成数据库选型与部署方案 20 页汇报 PPT |
| `export_bundle.py` | 导出已接受最优链路为版本化推理 bundle |
| `verify_execution.py` | 验收检查 |
| `fetch_reanalysis.py` | NASA POWER 再分析取数（坐标未提供，管线待命） |
| `sync_optimization_obsidian.py` | 旧的 Obsidian 同步脚本，现以手工同步为准 |

## 已接受节点（对应注册表 9 个里程碑）

`run_kpower_ablation.py`、`run_validation_blend.py`、`run_spatial_nwp.py`、`run_temporal_nwp.py`、`run_shared_model.py`、`run_curve_residual.py`、`run_mlp_shared.py`、`run_mlp_tuned.py`（结构选择）、`run_mlp_seedavg_tuned.py`（节点 9，当前最优）。

## 被否轮次（结论见 docs/研究记录/optimization-tricks.md）

`run_site_blend.py`、`run_lead_blend.py`、`run_joint_blend.py`、`run_diversity_blend.py`、`run_prediction_calibration.py`、`run_kernel_ridge.py`、`run_hist_gradient.py`、`run_extra_trees.py`、`run_shared_capacity_search.py`、`run_shared_bagged.py`、`run_shared_median.py`、`run_mlp_seed_ensemble.py`、`run_mlp_mixed_ensemble.py`、`run_mlp_seed8_check.py`、`run_hurdle_shared.py`、`run_pooled_base.py`、`run_cross_history.py`、`run_day_context_shared.py`、`run_direct_full_year.py`、`run_direction_shared.py`、`run_vertical_shared.py`、`run_forecast_bias_shared.py`、`run_mean_stack_shared.py`、`run_expanded_refit.py`、`run_dispersion_features.py`、`run_lead_segments.py`、`run_rolling_residual_features.py`。

## 分析脚本

`run_meta_oof_feasibility.py`（2012 期内 OOF 可行性检查）、`run_meta_oof_reference.py`（分布对照）。

## 归档（不要重跑）

`archive/` 里是已作废节点与越界实验的脚本，见 `archive/README.md`。

## 运行约定

```sh
export PYTHONPYCACHEPREFIX=/tmp/wpf-pyc     # iCloud 内不落 __pycache__
export MPLCONFIGDIR=/tmp/wpf-mpl            # 画图需要可写字体缓存
.venv/bin/python scripts/<script>.py        # 或 uv run python scripts/<script>.py
```

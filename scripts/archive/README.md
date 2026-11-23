# 归档脚本（不要重跑）

这些脚本保留只为了**证据可追溯**，不要重新运行；它们产生的结论要么已被协议判定为越界，要么对应的节点已被作废。

| 脚本 | 状态 | 原因 |
|---|---|---|
| `run_shared_final_refit.py` | 节点已作废 | 把 2013 上半年标签并入最终拟合，违反严格协议 |
| `run_mlp_final_refit.py` | 节点已作废 | 同上 |
| `run_temporal_final_refit.py` | 节点已作废 | 同上（并依赖上面的 `past_oof`） |
| `run_meta_learner.py` | 设计越界 | 用验证集训练 meta-learner，未按协议执行，未产生任何指标 |

运行这些脚本会重新生成被作废或被否决的结果，并可能污染注册表，因此一律不要执行。当前有效链路见 `docs/归档/gefcom2014/optimization-progress.json`。

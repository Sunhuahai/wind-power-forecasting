"""Run predeclared same-row ablations and paired comparisons; never select on test."""
import argparse
import json
from pathlib import Path

import polars as pl
from sqlalchemy import text

from windpower.db.core import engine
from windpower.evaluation.metrics import metrics
from windpower.evaluation.paired import paired_comparison
from windpower.models.kpower_reproduction import run


def compare(folders, out):
    wide = []
    for site in range(1, 11):
        frames = {}
        train_keys = None
        for name, folder in folders.items():
            keys = pl.read_csv(folder / f"site_{site}/train_keys.csv")
            if train_keys is not None and not keys.equals(train_keys):
                raise ValueError(f"Training samples differ: {name}, zone {site}")
            train_keys = keys
            frame = pl.read_csv(folder / f"site_{site}/predictions.csv", try_parse_dates=True)
            frame = frame.sort("origin", "valid_at")
            if frames and not frame.select("origin", "valid_at", "lead", "actual").equals(
                next(iter(frames.values())).select("origin", "valid_at", "lead", "actual")
            ):
                raise ValueError(f"Test samples differ: {name}, zone {site}")
            frames[name] = frame
        combined = frames["C"].select("origin", "valid_at", "lead", "actual").with_columns(
            pl.lit(site).alias("site"),
            *[pl.Series(name, frame["prediction"]) for name, frame in frames.items()],
        )
        wide.append(combined)
    frame = pl.concat(wide)
    scores = [dict(model=name, **metrics(frame["actual"], frame[name])) for name in folders]
    pairs = [paired_comparison(frame, candidate, baseline=baseline)
             for candidate, baseline in [("neighbors", "C"), ("full", "C"), ("full", "neighbors")]]
    pl.DataFrame(scores).write_csv(out / "pooled_metrics.csv")
    pl.DataFrame(pairs).write_csv(out / "paired_comparisons.csv")
    frame.write_csv(out / "paired_predictions.csv")
    report = ["# 同样本 kPower 特征消融", "",
              "三组第二层训练样本键和测试标签逐行完全相同；同一超参数、验证早停。",
              "已有历史测试集上的探索性评价；未根据此结果调整参数。", "",
              "|模型|MAE|RMSE|小时|", "|---|---:|---:|---:|"]
    report += [f"|{r['model']}|{r['MAE']:.9f}|{r['RMSE']:.9f}|{r['n']}|" for r in scores]
    report += ["", "七日连续日历块配对 bootstrap，2000 次，同日全站共同重采样。",
               "区间不包含重训、新年份或新场站的不确定性。", "",
               "|候选−对照|RMSE 差|95% 区间|", "|---|---:|---|"]
    report += [f"|{r['candidate']}−{r['baseline']}|{r['delta_RMSE']:.9f}|"
               f"[{r['delta_RMSE_low']:.9f}, {r['delta_RMSE_high']:.9f}]|" for r in pairs]
    (out / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    (out / "complete.json").write_text(json.dumps(dict(scores=scores, pairs=pairs), indent=2))
    print(json.dumps(scores, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-id", required=True)
    args = parser.parse_args()
    if not args.experiment_id.replace("_", "").replace("-", "").isalnum():
        parser.error("experiment-id must contain only letters, numbers, underscores or hyphens")
    out = Path("artifacts/kpower_ablation") / args.experiment_id
    out.mkdir(parents=True, exist_ok=False)
    with engine().connect() as connection:
        version = connection.scalar(text("SHOW server_version"))
    (out / "protocol.json").write_text(json.dumps(dict(
        postgres=version, feature_sets=["C", "neighbors", "full"],
        selection="No test-guided parameter selection; exploratory historical benchmark",
    ), indent=2))
    folders = {name: run(list(range(1, 11)), f"{args.experiment_id}_{name}", name)
               for name in ["C", "neighbors", "full"]}
    compare(folders, out)


if __name__ == "__main__":
    main()

"""Fit one pooled convex blend on validation, freeze weights, then score test."""
import argparse
import json
from pathlib import Path

import polars as pl

from windpower.evaluation.metrics import metrics
from windpower.evaluation.paired import paired_comparison
from windpower.models.validation_blend import fit_weights

NAMES = ["C", "neighbors", "full"]


def load(folders, filename):
    sites = []
    for site in range(1, 11):
        frames = {name: pl.read_csv(folder / f"site_{site}" / filename, try_parse_dates=True)
                  .sort("origin", "valid_at") for name, folder in folders.items()}
        keys = frames["C"].select("origin", "valid_at", "lead", "actual")
        for name, frame in frames.items():
            if not keys.equals(frame.select(keys.columns)):
                raise ValueError(f"Mismatched {filename} labels/keys for {name} zone {site}")
        sites.append(keys.with_columns(pl.lit(site).alias("site"), *[
            pl.Series(name, frames[name]["prediction"]) for name in NAMES]))
    return pl.concat(sites)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ablation-id", required=True)
    args = parser.parse_args()
    if not args.ablation_id.replace("_", "").replace("-", "").isalnum():
        parser.error("Invalid ablation ID")
    parent = Path("artifacts/kpower_ablation") / args.ablation_id
    if not (parent / "complete.json").exists():
        raise ValueError("Ablation comparison must finish first")
    out = parent / "validation_blend"
    out.mkdir(exist_ok=False)
    folders = {name: Path("artifacts/kpower_reproduction") / f"{args.ablation_id}_{name}"
               for name in NAMES}
    validation = load(folders, "validation_predictions.csv")
    weights = fit_weights(validation.select(NAMES).to_numpy(), validation["actual"].to_numpy())
    # This persisted file precedes test loading, so test cannot affect fitting.
    spec = dict(weights=dict(zip(NAMES, weights.tolist())),
                objective="pooled validation MSE; global nonnegative weights summing to one",
                validation_samples=validation.height,
                validation_first=str(validation["valid_at"].min()),
                validation_last=str(validation["valid_at"].max()),
                source_folders={name: str(folder) for name, folder in folders.items()},
                caveat="Validation also used for base-model early stopping; historical test already reported")
    (out / "weights.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")
    test = load(folders, "predictions.csv")
    if validation["valid_at"].max() >= test["valid_at"].min():
        raise ValueError("Validation/test dates overlap")
    test = test.with_columns(pl.Series("blend", test.select(NAMES).to_numpy() @ weights))
    scores = [dict(model=name, **metrics(test["actual"], test[name])) for name in ["full", "blend"]]
    paired = paired_comparison(test, "blend", baseline="full")
    pl.DataFrame(scores).write_csv(out / "metrics.csv")
    test.write_csv(out / "predictions.csv")
    (out / "paired.json").write_text(json.dumps(paired, indent=2))
    (out / "report.md").write_text(
        "# 验证集凸融合\n\n全站共享非负权重，权重和为一；仅用验证 MSE 拟合。\n\n"
        + "权重：" + json.dumps(spec["weights"], ensure_ascii=False) + "\n\n"
        + "|模型|MAE|RMSE|\n|---|---:|---:|\n"
        + "\n".join(f"|{r['model']}|{r['MAE']:.9f}|{r['RMSE']:.9f}|" for r in scores)
        + f"\n\n融合−full RMSE 差：{paired['delta_RMSE']:.9f}；七日块配对 95% 区间："
        + f"[{paired['delta_RMSE_low']:.9f}, {paired['delta_RMSE_high']:.9f}]。\n"
        + "\n验证集也用于基模型早停；测试为已报告的历史基准，属于探索性评价。\n",
        encoding="utf-8")
    print(json.dumps(dict(**spec, scores=scores, paired=paired), indent=2), flush=True)


if __name__ == "__main__":
    main()

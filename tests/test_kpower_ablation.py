"""Ablations must reject different training rows or held-out labels."""
from datetime import datetime, timedelta
import importlib.util
from pathlib import Path

import polars as pl
import pytest


spec = importlib.util.spec_from_file_location(
    "run_kpower_ablation", Path(__file__).parents[1] / "scripts/run_kpower_ablation.py")
ablation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ablation)


@pytest.mark.parametrize("mismatch", ["training", "test"])
def test_reject_different_samples(tmp_path, mismatch):
    folders = {name: tmp_path / name for name in ["C", "neighbors", "full"]}
    origin = datetime(2013, 7, 1)
    for name, folder in folders.items():
        out = folder / "site_1"
        out.mkdir(parents=True)
        keys = pl.DataFrame(dict(origin=[origin], valid_at=[origin + timedelta(hours=1)]))
        training = keys
        if name == "neighbors" and mismatch == "training":
            training = keys.with_columns(pl.col("valid_at") + timedelta(hours=1))
        training.write_csv(out / "train_keys.csv")
        keys.with_columns(
            pl.lit(1).alias("lead"),
            pl.lit(.9 if name == "neighbors" and mismatch == "test" else .5).alias("actual"),
            pl.lit(.6).alias("prediction"),
        ).write_csv(out / "predictions.csv")
    with pytest.raises(ValueError, match="Training samples differ" if mismatch == "training" else "Test samples differ"):
        ablation.compare(folders, tmp_path)

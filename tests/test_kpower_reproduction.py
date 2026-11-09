"""Offline unit tests for the kPower-inspired reproduction.

The tests do not require PostgreSQL or the competition ZIP.
"""
from datetime import datetime, timedelta

import numpy as np
import polars as pl
import pytest

from windpower.models import kpower_reproduction as kp


def test_nwp_lookahead_never_crosses_daily_forecast_origin(monkeypatch):
    start = datetime(2012, 1, 1, 1)
    records = []
    for i in range(48):
        valid = start + timedelta(hours=i)
        speed = 1.0 if i < 24 else 8.0
        records.append(dict(valid_at=valid, u10=speed, v10=0.0,
                            u100=speed, v100=0.0))

    class Cursor:
        def mappings(self):
            return self
        def all(self):
            return records

    class Context:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def execute(self, *args, **kwargs):
            return Cursor()

    class Engine:
        def connect(self):
            return Context()

    monkeypatch.setattr(kp, "engine", lambda: Engine())
    panel = kp.nwp_panel(1)
    assert panel.height == 48
    a = panel.filter((pl.col("origin") == datetime(2012, 1, 1)) &
                     (pl.col("lead") == 24)).row(0, named=True)
    b = panel.filter((pl.col("origin") == datetime(2012, 1, 2)) &
                     (pl.col("lead") == 1)).row(0, named=True)
    assert a["energy100_shift_+3"] == pytest.approx(1.0)
    assert a["energy100_mean5"] == pytest.approx(1.0)
    assert b["energy100_shift_-3"] == pytest.approx(512.0)
    assert b["energy100_mean5"] == pytest.approx(512.0)


def test_expanding_oof_never_uses_future_labels(monkeypatch):
    monkeypatch.setattr(kp, "MIN_TRAIN_DAYS", 2)
    monkeypatch.setattr(kp, "OOF_BLOCK_DAYS", 1)

    class MeanModel:
        def fit(self, x, y):
            self.mean = float(np.mean(y))
            return self
        def predict(self, x):
            return np.repeat(self.mean, len(x))

    monkeypatch.setattr(kp, "qmodel", lambda *_args, **_kwargs: MeanModel())
    dates = [datetime(2012, 1, 1) + timedelta(days=i) for i in range(5)]
    data = []
    for i, origin in enumerate(dates):
        row = {"origin": origin, "valid_at": origin + timedelta(hours=1),
               "actual": [0.0, 1.0, 0.0, 0.5, 0.9][i]}
        row.update({feature: 1.0 for feature in kp.STAGE1})
        data.append(row)
    tr = pl.DataFrame(data)
    panel = tr.drop("actual")
    pred = kp.stage1_oof(tr, panel, {}, 1)
    first = pred.filter(pl.col("origin") == dates[2])["stage1_1"][0]
    assert first == pytest.approx(0.5)
    altered = tr.with_columns(
        pl.when(pl.col("origin") >= dates[2]).then(0.99)
          .otherwise(pl.col("actual")).alias("actual"))
    pred2 = kp.stage1_oof(altered, panel, {}, 1)
    first2 = pred2.filter(pl.col("origin") == dates[2])["stage1_1"][0]
    assert first2 == pytest.approx(first)


def test_pinball_and_cross_zone_missing_detection():
    assert kp.pinball(np.array([1.0]), np.array([0.0]), 0.1) == pytest.approx(0.1)
    assert kp.pinball(np.array([0.0]), np.array([1.0]), 0.1) == pytest.approx(0.9)
    keys = pl.DataFrame({"origin": [datetime(2012, 1, 1)],
                         "valid_at": [datetime(2012, 1, 1, 1)]})
    frames = [keys.with_columns(pl.lit(float(i)).alias(f"stage1_{i}"))
              for i in range(1, 11)]
    combined = kp.combine(frames)
    assert combined.height == 1
    assert kp.add_cross_site(keys, combined).height == 1
    with pytest.raises(ValueError, match="Missing cross-zone"):
        kp.add_cross_site(keys, kp.combine(frames[:-1]))

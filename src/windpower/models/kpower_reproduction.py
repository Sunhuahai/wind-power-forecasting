"""kPower-inspired GEFCom2014 experiment: NWP neighborhoods, temporal OOF stacking, quantiles.

Method reimplementation, NOT the original winner's source code or official contest replay.
The only inputs at inference are the 24-h NWP bundle and (for the final target)
historical power strictly before each origin. All stage-one cross-site inputs are
predictions, never observed power at the target timestamp.
"""
from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import joblib
import numpy as np
import polars as pl
from lightgbm import LGBMRegressor, early_stopping
from sqlalchemy import select, text

from windpower.data.ingest import origin_for
from windpower.db.core import engine, upsert
from windpower.db.schema import model_runs, predictions
from windpower.evaluation.metrics import metrics, pooled_metrics
from windpower.features.build import B, C, build
from windpower.models.train import config, partitions

SHIFTS = (-3, -1, 1, 3)
EXTRA = [f"energy100_shift_{d:+d}" for d in SHIFTS] + [
    "energy100_mean3", "energy100_mean5", "energy100_gradient"
]
STAGE1 = B + EXTRA
STAGE2 = C + EXTRA + [f"stage1_{i}" for i in range(1, 11)]
QUANTILES = (0.1, 0.5, 0.9)
MIN_TRAIN_DAYS = 90
OOF_BLOCK_DAYS = 30
OOF_TREES = 160


def nwp_panel(site: int) -> pl.DataFrame:
    """Complete daily, forecast-only NWP panel; no power history or target labels."""
    with engine().connect() as c:
        rows = c.execute(text(
            "SELECT valid_at,u10,v10,u100,v100 FROM nwp_forecasts "
            "WHERE farm_id=:site ORDER BY valid_at"
        ), {"site": site}).mappings().all()
    days: dict[datetime, dict[int, dict]] = {}
    for r in rows:
        valid = r["valid_at"]
        o = origin_for(valid)
        lead = int((valid - o).total_seconds() // 3600)
        if not 1 <= lead <= 24:
            continue
        vals = np.asarray([r["u10"], r["v10"], r["u100"], r["v100"]], dtype=float)
        if not np.isfinite(vals).all():
            continue
        days.setdefault(o, {})[lead] = dict(zip(("u10", "v10", "u100", "v100"), vals))

    records = []
    for origin, day in sorted(days.items()):
        if set(day) != set(range(1, 25)):
            continue
        e100 = np.array([np.hypot(day[h]["u100"], day[h]["v100"]) ** 3
                         for h in range(1, 25)])
        for lead in range(1, 25):
            valid = origin + timedelta(hours=lead)
            r = dict(day[lead], origin=origin, valid_at=valid, lead=lead)
            for height in (10, 100):
                u, v = r[f"u{height}"], r[f"v{height}"]
                speed = float(np.hypot(u, v))
                r[f"speed{height}"] = speed
                r[f"dir{height}_sin"] = v / max(speed, 1e-8)
                r[f"dir{height}_cos"] = u / max(speed, 1e-8)
            r["speed100_cube"] = r["speed100"] ** 3
            r["shear"] = np.log(max(r["speed100"], 1e-6) /
                                max(r["speed10"], 1e-6)) / np.log(10)
            for name, theta in (
                ("hour", 2 * np.pi * valid.hour / 24),
                ("year", 2 * np.pi * (valid.timetuple().tm_yday - 1) / 365.25),
            ):
                r[f"{name}_sin"] = float(np.sin(theta))
                r[f"{name}_cos"] = float(np.cos(theta))
            i = lead - 1
            for delta in SHIFTS:
                r[f"energy100_shift_{delta:+d}"] = float(
                    e100[np.clip(i + delta, 0, 23)])
            r["energy100_mean3"] = float(np.mean(e100[max(0, i-1):min(24, i+2)]))
            r["energy100_mean5"] = float(np.mean(e100[max(0, i-2):min(24, i+3)]))
            r["energy100_gradient"] = float(
                e100[min(23, i+1)] - e100[max(0, i-1)])
            records.append(r)
    if not records:
        raise ValueError(f"No complete NWP days for site {site}")
    return pl.DataFrame(records).sort(["origin", "valid_at"])


def attach(frame: pl.DataFrame, panel: pl.DataFrame) -> pl.DataFrame:
    out = frame.join(panel.select(["origin", "valid_at", *EXTRA]),
                     on=["origin", "valid_at"], how="left", validate="1:1")
    if out.select(pl.any_horizontal(pl.col(EXTRA).is_null())).to_series().any():
        raise ValueError("Missing NWP neighborhood features")
    return out.sort(["origin", "valid_at"])


def qmodel(params: dict, alpha: float, trees: int | None = None) -> LGBMRegressor:
    kw = {**params, "objective": "quantile", "alpha": alpha}
    if trees is not None:
        kw["n_estimators"] = trees
    return LGBMRegressor(**kw)


def fit_stage1(train: pl.DataFrame, validation: pl.DataFrame,
               params: dict) -> LGBMRegressor:
    m = qmodel(params, 0.5)
    m.fit(train.select(STAGE1).to_numpy(), train["actual"].to_numpy(),
          eval_set=[(validation.select(STAGE1).to_numpy(),
                     validation["actual"].to_numpy())],
          callbacks=[early_stopping(30, verbose=False)])
    return m


def stage1_oof(train: pl.DataFrame, panel: pl.DataFrame,
               params: dict, site: int) -> pl.DataFrame:
    """Expanding time folds; predictions for a day come only from earlier days."""
    origins = train["origin"].unique().sort().to_list()
    blocks = []
    for start in range(MIN_TRAIN_DAYS, len(origins), OOF_BLOCK_DAYS):
        cutoff = origins[start]
        hold = origins[start:start + OOF_BLOCK_DAYS]
        tr = train.filter(pl.col("origin") < cutoff)
        va = panel.filter(pl.col("origin").is_in(hold))
        if tr.is_empty() or va.is_empty():
            continue
        m = qmodel(params, 0.5, OOF_TREES)
        m.fit(tr.select(STAGE1).to_numpy(), tr["actual"].to_numpy())
        pred = np.clip(m.predict(va.select(STAGE1).to_numpy()), 0, 1)
        blocks.append(va.select("origin", "valid_at").with_columns(
            pl.Series(f"stage1_{site}", pred)))
    if not blocks:
        raise ValueError("Not enough training days for rolling stage-one OOF")
    return pl.concat(blocks).sort(["origin", "valid_at"])


def combine(frame_list: list[pl.DataFrame]) -> pl.DataFrame:
    out = frame_list[0]
    for f in frame_list[1:]:
        out = out.join(f, on=["origin", "valid_at"], how="inner", validate="1:1")
    return out.sort(["origin", "valid_at"])


def add_cross_site(frame: pl.DataFrame, cross: pl.DataFrame) -> pl.DataFrame:
    out = frame.join(cross, on=["origin", "valid_at"], how="left",
                     validate="1:1").sort(["origin", "valid_at"])
    cols = [f"stage1_{i}" for i in range(1, 11)]
    if out.select(pl.any_horizontal(pl.col(cols).is_null())).to_series().any():
        raise ValueError("Missing cross-zone OOF or inference features")
    return out


def pinball(y: np.ndarray, q: np.ndarray, alpha: float) -> float:
    d = np.asarray(y) - np.asarray(q)
    return float(np.mean(np.maximum(alpha*d, (alpha-1)*d)))


def run(sites: list[int], experiment_id: str) -> Path:
    cfg = config()
    out = Path("artifacts/kpower_reproduction") / experiment_id
    if out.exists():
        raise FileExistsError(f"Experiment already exists: {out}")
    out.mkdir(parents=True)
    panels, parts, stage1_models, oofs, all_preds = {}, {}, {}, [], []
    for site in range(1, 11):
        panels[site] = nwp_panel(site)
        parts[site] = {name: attach(f, panels[site])
                       for name, f in partitions(build(site)).items()}
        tr, va = parts[site]["train"], parts[site]["validation"]
        print(f"Preparing stage one for zone {site}", flush=True)
        oofs.append(stage1_oof(tr, panels[site], cfg["lightgbm"], site))
        stage1_models[site] = fit_stage1(tr, va, cfg["lightgbm"])
        pred = np.clip(stage1_models[site].predict(
            panels[site].select(STAGE1).to_numpy()), 0, 1)
        all_preds.append(panels[site].select("origin", "valid_at").with_columns(
            pl.Series(f"stage1_{site}", pred)))
    train_cross = combine(oofs)
    future_cross = combine(all_preds)
    summary = []
    for site in sites:
        # A single inner join restricts stage-two training to honest OOF periods.
        tr = parts[site]["train"].join(
            train_cross, on=["origin", "valid_at"], how="inner", validate="1:1"
        ).sort(["origin", "valid_at"])
        va = add_cross_site(parts[site]["validation"], future_cross)
        te = add_cross_site(parts[site]["test"], future_cross)
        if tr.is_empty():
            raise ValueError(f"No OOF stage-two examples for site {site}")
        x, y = tr.select(STAGE2).to_numpy(), tr["actual"].to_numpy()
        xv, yv = va.select(STAGE2).to_numpy(), va["actual"].to_numpy()
        xt, yt = te.select(STAGE2).to_numpy(), te["actual"].to_numpy()
        # Verify identical held-out targets against the frozen original C run.
        old_ids = json.loads(Path(f"artifacts/site_{site}/latest_runs.json").read_text())
        old_path = Path(f"artifacts/site_{site}/{old_ids[-1]}_predictions.csv")
        if not old_path.exists():
            raise FileNotFoundError(f"Frozen baseline CSV missing: {old_path}")
        frozen = pl.read_csv(old_path, try_parse_dates=True).sort(
            ["origin", "valid_at"])
        if (frozen.select("origin", "valid_at").rows() !=
                te.select("origin", "valid_at").rows() or
                not np.array_equal(frozen["actual"].to_numpy(), yt)):
            raise ValueError("Test keys/labels differ from frozen C baseline")
        point = LGBMRegressor(**cfg["lightgbm"])
        point.fit(x, y, eval_set=[(xv, yv)],
                  callbacks=[early_stopping(30, verbose=False)])
        p = np.clip(point.predict(xt), 0, 1)
        qs = {}
        for alpha in QUANTILES:
            m = qmodel(cfg["lightgbm"], alpha)
            m.fit(x, y, eval_set=[(xv, yv)],
                  callbacks=[early_stopping(30, verbose=False)])
            qs[alpha] = m
        # Avoid quantile crossing after independent fits.
        array = np.maximum.accumulate(np.column_stack([
            np.clip(qs[q].predict(xt), 0, 1) for q in QUANTILES
        ]), axis=1)
        s = metrics(yt, p)
        score = float(np.mean([
            pinball(yt, array[:, i], q) for i, q in enumerate(QUANTILES)
        ]))
        row = dict(site=site, **s, pinball_3q=score,
                   coverage80=float(np.mean((yt >= array[:, 0]) &
                                            (yt <= array[:, 2]))),
                   stage2_train_samples=tr.height, test_samples=te.height)
        summary.append(row)
        site_out = out / f"site_{site}"
        site_out.mkdir()
        run_id = str(uuid.uuid4())
        bundle = dict(point=point, quantiles=qs, stage1=stage1_models,
                      stage1_columns=STAGE1, stage2_columns=STAGE2,
                      algorithm="kPower-inspired, not authors original code",
                      oof=dict(min_train_days=MIN_TRAIN_DAYS,
                               block_days=OOF_BLOCK_DAYS, trees=OOF_TREES))
        artifact = site_out / f"{run_id}.joblib"
        joblib.dump(bundle, artifact)
        result = te.select("origin", "valid_at", "lead", "actual").with_columns(
            pl.Series("prediction", p),
            pl.Series("q10", array[:, 0]),
            pl.Series("q50", array[:, 1]),
            pl.Series("q90", array[:, 2])
        )
        result.write_csv(site_out / "predictions.csv")
        with engine().begin() as c:
            upsert(c, model_runs, [dict(
                id=run_id, farm_id=site, model="kpower_inspired_point",
                feature_group="NWP-neighbors+cross-zone-OOF+C",
                created_at=datetime.now(), config=dict(
                    base_config=cfg, stage1_columns=STAGE1, stage2_columns=STAGE2,
                    oof_start_days=MIN_TRAIN_DAYS,
                    oof_block_days=OOF_BLOCK_DAYS,
                    note="3 quantiles only; not the original 99-quantile contest score"),
                artifact=str(artifact), metrics=s
            )])
            upsert(c, predictions, [
                dict(run_id=run_id, origin=r["origin"], valid_at=r["valid_at"],
                     lead=r["lead"], actual=r["actual"],
                     prediction=r["prediction"])
                for r in result.iter_rows(named=True)
            ])
        print(f"Zone {site} kPower-inspired RMSE={s['RMSE']:.6f}, "
              f"MAE={s['MAE']:.6f}, 3q pinball={score:.6f}", flush=True)
    pooled = pooled_metrics(summary)
    pl.DataFrame(summary).write_csv(out / "site_metrics.csv")
    (out / "summary.json").write_text(json.dumps(dict(
        sites=sites, pooled=pooled,
        pinball_3q_weighted=float(sum(v["pinball_3q"]*v["n"] for v in summary) /
                                  sum(v["n"] for v in summary)),
        protocol="fixed project train/val/test; expanding-fold stage-one OOF",
        non_equivalence="not original winner code, 99q score, or 12 rolling tasks",
        original_baseline_RMSE=0.17045420423783827,
    ), indent=2), encoding="utf-8")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sites", default="1", help="Comma-separated zone IDs or all")
    ap.add_argument("--experiment-id", default=None)
    args = ap.parse_args()
    sites = list(range(1, 11)) if args.sites == "all" else [
        int(i) for i in args.sites.split(",")]
    if not sites or any(i not in range(1, 11) for i in sites):
        ap.error("sites must be 1..10")
    experiment_id = args.experiment_id or (
        "kpower_" + datetime.now().strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:6])
    print(run(sites, experiment_id))


if __name__ == "__main__":
    main()

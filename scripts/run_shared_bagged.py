"""Bagged, seed-averaged shared LightGBM branch under the strict protocol.

Hypothesis: the 26 percent-weight shared model is a single deterministic
LightGBM trained on 65,760 OOF rows; row and column subsampling plus seed
averaging should reduce its variance without touching the fitting data.
"""
import json
from pathlib import Path

import joblib
import numpy as np
import polars as pl
from lightgbm import LGBMRegressor

from windpower.features.build import build
from windpower.features.cross_nwp import temporal_spatial_panel, attach_temporal, COLUMNS, TEMPORAL_COLUMNS
from windpower.models.train import config, partitions
from windpower.models import kpower_reproduction as kp
from windpower.evaluation.metrics import metrics
from windpower.evaluation.paired import paired_comparison

ID = 'ablation_20261114_v1'
PARENT = Path('artifacts/kpower_ablation') / ID
OUT = PARENT / 'shared_bagged_v1'
FEATURES = kp.STAGE2 + COLUMNS + TEMPORAL_COLUMNS + ['farm']
KEYS = ['origin', 'valid_at', 'lead', 'actual', 'site']
SEEDS = [42, 7, 2024, 101, 31337]


def main():
    registry = json.loads(Path('docs/归档/gefcom2014/optimization-progress.json').read_text())
    if registry['milestones'][-1]['id'] != 'mlp_seedavg_tuned':
        raise ValueError('Baseline changed; refusing to run')
    OUT.mkdir(exist_ok=False)
    cfg = config()
    shared = joblib.load(PARENT / 'shared_model_v1/model.joblib')
    rounds = int(shared['model'].best_iteration_)
    w_m = json.loads((PARENT / 'mlp_seedavg_tuned_v1/weights.json').read_text())
    w_c = json.loads((PARENT / 'curve_residual_v1/weights.json').read_text())
    w_s = json.loads((PARENT / 'shared_model_v1/weights.json').read_text())
    coefficient = w_m['baseline'] * w_c['baseline'] * w_s['shared']
    protocol = dict(
        method='Bagged seed-averaged shared LightGBM on the same 2012 OOF panel',
        seeds=SEEDS, frozen_rounds=rounds, fixed_branch_coefficient=coefficient,
        params=dict(cfg['lightgbm'], num_leaves=31, min_child_samples=80,
                    bagging_fraction=0.8, bagging_freq=1, feature_fraction=0.8),
        selection='No round or weight re-selection; branch coefficient frozen from the accepted chain',
        caveat='Strict protocol: 2012-only training; validation used earlier for this branch coefficient')
    (OUT / 'protocol.json').write_text(json.dumps(protocol, indent=2))

    stage1 = joblib.load(next(Path(f'artifacts/kpower_reproduction/{ID}_full/site_1').glob('*.joblib')))['stage1']
    panels, parts, oofs, crosses = {}, {}, [], []
    for site in range(1, 11):
        panel = kp.nwp_panel(site)
        panels[site] = panel
        parts[site] = {k: kp.attach(v, panel) for k, v in partitions(build(site)).items()}
        oofs.append(kp.stage1_oof(parts[site]['train'], panel, cfg['lightgbm'], site))
        crosses.append(panel.select('origin', 'valid_at').with_columns(pl.Series(f'stage1_{site}',
            np.clip(stage1[site].predict(panel.select(kp.STAGE1).to_numpy()), 0, 1))))
        print(f'Prepared zone {site}', flush=True)
    spatial = temporal_spatial_panel(panels)
    cross_train, cross_future = kp.combine(oofs), kp.combine(crosses)

    trains = []
    for site in range(1, 11):
        tr = parts[site]['train'].join(cross_train, on=['origin', 'valid_at'], how='inner', validate='1:1') \
            .sort('origin', 'valid_at')
        expected = pl.read_csv(f'artifacts/kpower_reproduction/{ID}_full/site_{site}/train_keys.csv', try_parse_dates=True)
        if not tr.select('origin', 'valid_at').equals(expected):
            raise ValueError('Training keys differ')
        trains.append(attach_temporal(tr, spatial).with_columns(pl.lit(site).alias('farm')))
    tr = pl.concat(trains)
    x, y = tr.select(FEATURES).to_numpy(), tr['actual'].to_numpy()
    del trains

    tests = []
    for site in range(1, 11):
        tests.append(attach_temporal(kp.add_cross_site(parts[site]['test'], cross_future), spatial)
                     .with_columns(pl.lit(site).alias('farm'), pl.lit(site).alias('site')))
    te = pl.concat(tests)
    x_te = te.select(FEATURES).to_numpy()

    previous = pl.read_csv(PARENT / 'mlp_seedavg_tuned_v1/predictions.csv', try_parse_dates=True)
    if not te.select(KEYS).equals(previous.select(KEYS)):
        raise ValueError('Test keys/labels differ')
    old_shared = pl.read_csv(PARENT / 'shared_model_v1/predictions.csv', try_parse_dates=True)

    members, preds = [], []
    for seed in SEEDS:
        params = dict(protocol['params'], n_estimators=rounds, random_state=seed)
        model = LGBMRegressor(**params)
        model.fit(x, y, categorical_feature=[len(FEATURES) - 1])
        members.append(model)
        pred = np.clip(model.predict(x_te), 0, 1)
        preds.append(pred)
        print(f'seed {seed} fitted, single-model test RMSE {np.sqrt(np.mean((pred - te["actual"].to_numpy()) ** 2)):.6f}',
              flush=True)
    bagged = np.clip(np.mean(preds, axis=0), 0, 1)
    joblib.dump(dict(models=members, columns=FEATURES, protocol=protocol), OUT / 'model.joblib')

    retained = previous['mlp_seedavg_blend'].to_numpy() - coefficient * old_shared['shared'].to_numpy()
    if (retained < -1e-12).any() or (retained > 1 - coefficient + 1e-12).any():
        raise ValueError('Invalid branch decomposition')
    result = te.select(KEYS).with_columns(
        pl.Series('baseline', previous['mlp_seedavg_blend']),
        pl.Series('shared_bagged', bagged),
        pl.Series('shared_bagged_chain', np.clip(retained + coefficient * bagged, 0, 1)))
    scores = [dict(model=n, **metrics(result['actual'], result[n]))
              for n in ['baseline', 'shared_bagged', 'shared_bagged_chain']]
    paired = paired_comparison(result, 'shared_bagged_chain')
    accepted = scores[-1]['RMSE'] < scores[0]['RMSE'] and paired['delta_RMSE_high'] < 0
    single = [np.sqrt(np.mean((p - te['actual'].to_numpy()) ** 2)) for p in preds]
    (OUT / 'weights.json').write_text(json.dumps(dict(
        shared_branch=coefficient, frozen_rounds=rounds, seeds=SEEDS,
        train_samples=int(tr.height),
        single_seed_test_rmse=single,
        bagged_branch_test_rmse=float(scores[1]['RMSE'])), indent=2))
    pl.DataFrame(scores).write_csv(OUT / 'metrics.csv')
    (OUT / 'paired.json').write_text(json.dumps(paired, indent=2))
    (OUT / 'decision.json').write_text(json.dumps(dict(accepted=accepted, scores=scores), indent=2))
    if accepted:
        result.write_csv(OUT / 'predictions.csv')
    print(json.dumps(dict(accepted=accepted, scores=scores, paired=paired), indent=2), flush=True)


if __name__ == '__main__':
    main()

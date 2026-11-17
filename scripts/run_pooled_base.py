"""Pooled ten-site version of the base two-layer branch.

Hypothesis: milestone 5 showed that pooling the ten sites into one categorical
LightGBM was the single largest step of the whole history. The base C +
neighbors + full branch is still trained per site (about 6.5k rows each) and
carries 8.8 percent of the final combination, so replacing its "full" part
with a pooled ten-site model should transfer part of that gain.
"""
import json
from pathlib import Path

import joblib
import numpy as np
import polars as pl
from lightgbm import LGBMRegressor, early_stopping

from windpower.features.build import build
from windpower.features.cross_nwp import temporal_spatial_panel, attach_temporal, COLUMNS, TEMPORAL_COLUMNS
from windpower.models.train import config, partitions
from windpower.models import kpower_reproduction as kp
from windpower.evaluation.metrics import metrics
from windpower.evaluation.paired import paired_comparison

ID = 'ablation_20261114_v1'
PARENT = Path('artifacts/kpower_ablation') / ID
OUT = PARENT / 'pooled_base_v1'
FEATURES = kp.STAGE2 + ['farm']
KEYS = ['origin', 'valid_at', 'lead', 'actual', 'site']


def main():
    registry = json.loads(Path('docs/研究记录/optimization-progress.json').read_text())
    if registry['milestones'][-1]['id'] != 'mlp_seedavg_tuned':
        raise ValueError('Baseline changed; refusing to run')
    OUT.mkdir(exist_ok=False)
    cfg = config()
    params = dict(cfg['lightgbm'], num_leaves=31, min_child_samples=80, n_estimators=800)
    w_blend = json.loads((PARENT / 'validation_blend/weights.json').read_text())['weights']
    w_sp = json.loads((PARENT / 'spatial_nwp_v1/weights.json').read_text())
    w_t = json.loads((PARENT / 'temporal_nwp_v1/weights.json').read_text())
    w_s = json.loads((PARENT / 'shared_model_v1/weights.json').read_text())
    w_c = json.loads((PARENT / 'curve_residual_v1/weights.json').read_text())
    w_m = json.loads((PARENT / 'mlp_seedavg_tuned_v1/weights.json').read_text())
    coefficient = (w_blend['full'] * w_sp['baseline'] * w_t['baseline'] * w_s['baseline']
                   * w_c['baseline'] * w_m['baseline'])
    protocol = dict(
        method='Pooled ten-site categorical LightGBM replacing the per-site full stage-two branch',
        features=FEATURES, categorical_feature='farm', params=params,
        fixed_branch_coefficient=coefficient,
        selection='Pooled validation early stopping; branch coefficient frozen from the accepted chain',
        caveat='Strict 2012-only training; exploratory historical test')
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
    del spatial

    trains, validations, tests = [], [], []
    for site in range(1, 11):
        tr = parts[site]['train'].join(cross_train, on=['origin', 'valid_at'], how='inner', validate='1:1') \
            .sort('origin', 'valid_at')
        expected = pl.read_csv(f'artifacts/kpower_reproduction/{ID}_full/site_{site}/train_keys.csv', try_parse_dates=True)
        if not tr.select('origin', 'valid_at').equals(expected):
            raise ValueError('Training keys differ')
        trains.append(tr.with_columns(pl.lit(site).alias('farm')))
        validations.append(kp.add_cross_site(parts[site]['validation'], cross_future)
                           .with_columns(pl.lit(site).alias('farm'), pl.lit(site).alias('site')))
        tests.append(kp.add_cross_site(parts[site]['test'], cross_future)
                     .with_columns(pl.lit(site).alias('farm'), pl.lit(site).alias('site')))
    tr, va, te = pl.concat(trains), pl.concat(validations), pl.concat(tests)
    model = LGBMRegressor(**params)
    model.fit(tr.select(FEATURES).to_numpy(), tr['actual'].to_numpy(),
              categorical_feature=[len(FEATURES) - 1],
              eval_set=[(va.select(FEATURES).to_numpy(), va['actual'].to_numpy())],
              callbacks=[early_stopping(30, verbose=False)])
    print(f'pooled rounds={model.best_iteration_} train={tr.height}', flush=True)
    joblib.dump(dict(model=model, columns=FEATURES, protocol=protocol), OUT / 'model.joblib')

    previous = pl.read_csv(PARENT / 'mlp_seedavg_tuned_v1/predictions.csv', try_parse_dates=True)
    components = pl.read_csv(PARENT / 'paired_predictions.csv', try_parse_dates=True)
    old_full = components.select(KEYS + ['full'])
    new_full = te.select(KEYS).with_columns(pl.Series('full_pooled',
        np.clip(model.predict(te.select(FEATURES).to_numpy()), 0, 1)))
    joined = previous.join(old_full, on=KEYS, how='inner', validate='1:1') \
        .join(new_full, on=KEYS, how='inner', validate='1:1')
    if joined.height != 42432:
        raise ValueError(f'Test keys differ: {joined.height}')
    retained = joined['mlp_seedavg_blend'].to_numpy() - coefficient * joined['full'].to_numpy()
    if (retained < -1e-12).any() or (retained > 1 - coefficient + 1e-12).any():
        raise ValueError('Invalid branch decomposition')
    result = joined.select(KEYS).with_columns(
        pl.Series('baseline', joined['mlp_seedavg_blend']),
        pl.Series('full_pooled', joined['full_pooled']),
        pl.Series('pooled_chain', np.clip(retained + coefficient * joined['full_pooled'].to_numpy(), 0, 1)))
    scores = [dict(model=n, **metrics(result['actual'], result[n])) for n in ['baseline', 'full_pooled', 'pooled_chain']]
    paired = paired_comparison(result, 'pooled_chain')
    accepted = scores[-1]['RMSE'] < scores[0]['RMSE'] and paired['delta_RMSE_high'] < 0
    (OUT / 'weights.json').write_text(json.dumps(dict(
        full_branch=coefficient, pooling_weights=w_blend, rounds=int(model.best_iteration_),
        train_samples=int(tr.height), validation_samples=int(va.height)), indent=2))
    pl.DataFrame(scores).write_csv(OUT / 'metrics.csv')
    (OUT / 'paired.json').write_text(json.dumps(paired, indent=2))
    (OUT / 'decision.json').write_text(json.dumps(dict(accepted=accepted, scores=scores), indent=2))
    if accepted:
        result.write_csv(OUT / 'predictions.csv')
    print(json.dumps(dict(accepted=accepted, scores=scores, paired=paired), indent=2), flush=True)


if __name__ == '__main__':
    main()

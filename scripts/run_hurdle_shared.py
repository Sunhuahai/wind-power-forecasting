"""Two-part (hurdle) shared model for the near-calm point mass.

Hypothesis: 8.7 percent of test hours have actual power below 0.005 and 6.6
percent are exactly zero. A single L2 tree smooths across that boundary; a
classifier for "near-calm" times a regressor for the generating regime may
estimate the conditional mean better. Same 2012 OOF rows, same features.
"""
import json
from pathlib import Path

import joblib
import numpy as np
import polars as pl
from lightgbm import LGBMClassifier, LGBMRegressor, early_stopping

from windpower.features.build import build
from windpower.features.cross_nwp import temporal_spatial_panel, attach_temporal, COLUMNS, TEMPORAL_COLUMNS
from windpower.models.train import config, partitions
from windpower.models import kpower_reproduction as kp
from windpower.evaluation.metrics import metrics
from windpower.evaluation.paired import paired_comparison

ID = 'ablation_20261114_v1'
PARENT = Path('artifacts/kpower_ablation') / ID
OUT = PARENT / 'hurdle_shared_v1'
FEATURES = kp.STAGE2 + COLUMNS + TEMPORAL_COLUMNS + ['farm']
KEYS = ['origin', 'valid_at', 'lead', 'actual', 'site']
TAU = 0.005


def main():
    registry = json.loads(Path('docs/optimization-progress.json').read_text())
    if registry['milestones'][-1]['id'] != 'mlp_seedavg_tuned':
        raise ValueError('Baseline changed; refusing to run')
    OUT.mkdir(exist_ok=False)
    cfg = config()
    params = dict(cfg['lightgbm'], num_leaves=31, min_child_samples=80, n_estimators=800)
    protocol = dict(
        method='Two-part hurdle shared model replacing the single L2 shared branch',
        threshold=TAU, features=FEATURES, categorical_feature='farm', params=params,
        selection='Validation early stopping for both parts; branch coefficient frozen from the accepted chain',
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

    trains, validations = [], []
    for site in range(1, 11):
        tr = parts[site]['train'].join(cross_train, on=['origin', 'valid_at'], how='inner', validate='1:1') \
            .sort('origin', 'valid_at')
        expected = pl.read_csv(f'artifacts/kpower_reproduction/{ID}_full/site_{site}/train_keys.csv', try_parse_dates=True)
        if not tr.select('origin', 'valid_at').equals(expected):
            raise ValueError('Training keys differ')
        trains.append(attach_temporal(tr, spatial).with_columns(pl.lit(site).alias('farm')))
        validations.append(attach_temporal(kp.add_cross_site(parts[site]['validation'], cross_future), spatial)
                           .with_columns(pl.lit(site).alias('farm'), pl.lit(site).alias('site')))
    tr, va = pl.concat(trains), pl.concat(validations)
    x, y = tr.select(FEATURES).to_numpy(), tr['actual'].to_numpy()
    xv, yv = va.select(FEATURES).to_numpy(), va['actual'].to_numpy()
    cat = [len(FEATURES) - 1]

    calm_tr = y < TAU
    calm_va = yv < TAU
    mean_calm = float(y[calm_tr].mean())
    clf = LGBMClassifier(**params)
    clf.fit(x, (~calm_tr).astype(int), eval_set=[(xv, (~calm_va).astype(int))], categorical_feature=cat,
            callbacks=[early_stopping(40, verbose=False)])
    reg = LGBMRegressor(**params)
    reg.fit(x[~calm_tr], y[~calm_tr], eval_set=[(xv[~calm_va], yv[~calm_va])], categorical_feature=cat,
            callbacks=[early_stopping(40, verbose=False)])
    p_gen = np.clip(clf.predict_proba(xv)[:, 1], 0, 1)
    reg_va = np.clip(reg.predict(xv), 0, 1)
    hurdle_va = np.clip(p_gen * reg_va + (1 - p_gen) * mean_calm, 0, 1)
    print(f'classifier rounds={clf.best_iteration_} regressor rounds={reg.best_iteration_} '
          f'calm share train={calm_tr.mean():.4f} validation RMSE={np.sqrt(np.mean((hurdle_va-yv)**2)):.6f}', flush=True)

    tests = []
    for site in range(1, 11):
        tests.append(attach_temporal(kp.add_cross_site(parts[site]['test'], cross_future), spatial)
                     .with_columns(pl.lit(site).alias('farm'), pl.lit(site).alias('site')))
    te = pl.concat(tests)
    previous = pl.read_csv(PARENT / 'mlp_seedavg_tuned_v1/predictions.csv', try_parse_dates=True)
    if not te.select(KEYS).equals(previous.select(KEYS)):
        raise ValueError('Test keys/labels differ')
    old_shared = pl.read_csv(PARENT / 'shared_model_v1/predictions.csv', try_parse_dates=True)
    w_m = json.loads((PARENT / 'mlp_seedavg_tuned_v1/weights.json').read_text())
    w_c = json.loads((PARENT / 'curve_residual_v1/weights.json').read_text())
    w_s = json.loads((PARENT / 'shared_model_v1/weights.json').read_text())
    coefficient = w_m['baseline'] * w_c['baseline'] * w_s['shared']

    xt = te.select(FEATURES).to_numpy()
    p_test = np.clip(clf.predict_proba(xt)[:, 1], 0, 1)
    reg_test = np.clip(reg.predict(xt), 0, 1)
    hurdle = np.clip(p_test * reg_test + (1 - p_test) * mean_calm, 0, 1)
    retained = previous['mlp_seedavg_blend'].to_numpy() - coefficient * old_shared['shared'].to_numpy()
    if (retained < -1e-12).any() or (retained > 1 - coefficient + 1e-12).any():
        raise ValueError('Invalid branch decomposition')
    result = te.select(KEYS).with_columns(
        pl.Series('baseline', previous['mlp_seedavg_blend']),
        pl.Series('hurdle', hurdle),
        pl.Series('hurdle_chain', np.clip(retained + coefficient * hurdle, 0, 1)))
    scores = [dict(model=n, **metrics(result['actual'], result[n])) for n in ['baseline', 'hurdle', 'hurdle_chain']]
    paired = paired_comparison(result, 'hurdle_chain')
    accepted = scores[-1]['RMSE'] < scores[0]['RMSE'] and paired['delta_RMSE_high'] < 0
    joblib.dump(dict(clf=clf, reg=reg, mean_calm=mean_calm, columns=FEATURES,
                     protocol=protocol, threshold=TAU), OUT / 'model.joblib')
    (OUT / 'weights.json').write_text(json.dumps(dict(
        shared_branch=coefficient, threshold=TAU, mean_calm=mean_calm,
        classifier_rounds=int(clf.best_iteration_), regressor_rounds=int(reg.best_iteration_),
        train_samples=int(tr.height), calm_share=float(calm_tr.mean())), indent=2))
    pl.DataFrame(scores).write_csv(OUT / 'metrics.csv')
    (OUT / 'paired.json').write_text(json.dumps(paired, indent=2))
    (OUT / 'decision.json').write_text(json.dumps(dict(accepted=accepted, scores=scores), indent=2))
    if accepted:
        result.write_csv(OUT / 'predictions.csv')
    print(json.dumps(dict(accepted=accepted, scores=scores, paired=paired), indent=2), flush=True)


if __name__ == '__main__':
    main()

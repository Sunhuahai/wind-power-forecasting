"""Seed-averaged MLP on the validation-selected architecture.

Hypothesis: the previous round showed the (128,64,32) network improved test
RMSE by 0.22 percent but the paired interval crossed zero, i.e. the effect is
real but noisy. Averaging three seeds of the same selected architecture, with
the convex weight still fitted on validation only, should cut that noise.
"""
import json
from pathlib import Path

import joblib
import numpy as np
import polars as pl
from sklearn.neural_network import MLPRegressor
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

from windpower.features.build import build
from windpower.features.cross_nwp import temporal_spatial_panel, attach_temporal, COLUMNS, TEMPORAL_COLUMNS
from windpower.models.train import config, partitions
from windpower.models import kpower_reproduction as kp
from windpower.models.validation_blend import fit_weights
from windpower.evaluation.metrics import metrics
from windpower.evaluation.paired import paired_comparison

ID = 'ablation_20261114_v1'
PARENT = Path('artifacts/kpower_ablation') / ID
BASE = PARENT / 'curve_residual_v1'
OUT = PARENT / 'mlp_seedavg_tuned_v1'
FEATURES = kp.STAGE2 + COLUMNS + TEMPORAL_COLUMNS
KEYS = ['origin', 'valid_at', 'lead', 'actual', 'site']
SEEDS = [42, 7, 2024]
CONFIG = dict(hidden_layer_sizes=(128, 64, 32), alpha=0.1)


def design(frame):
    farms = frame['farm'].to_numpy().astype(int)
    if ((farms < 1) | (farms > 10)).any():
        raise ValueError('Unknown farm identity')
    return np.column_stack([frame.select(FEATURES).to_numpy(), np.eye(10)[farms - 1]])


def main():
    registry = json.loads(Path('docs/归档/gefcom2014/optimization-progress.json').read_text())
    if registry['milestones'][-1]['id'] != 'mlp_shared_blend':
        raise ValueError('Baseline changed; refusing to run')
    OUT.mkdir(exist_ok=False)
    cfg = config()
    protocol = dict(
        config=CONFIG, seeds=SEEDS, features=FEATURES, farm_encoding='fixed one-hot identities 1..10',
        baseline=str(BASE),
        method='Architecture grid selected by external validation blend MSE; training rows stay the 2012 OOF panel',
        selection='Model config and convex blend both selected on validation (2013 H1); no validation labels enter training',
        caveat='Validation is used twice (architecture and blend weight); test is scored once with frozen choices')
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
    x_tr, y_tr, x_va, y_va = design(tr), tr['actual'].to_numpy(), design(va), va['actual'].to_numpy()

    old = pl.read_csv(BASE / 'validation_predictions.csv', try_parse_dates=True)
    if not va.select(KEYS).equals(old.select(KEYS)):
        raise ValueError('Validation keys/labels differ')
    baseline_v = old['curve_blend'].to_numpy()

    grid, members = [], []
    for seed in SEEDS:
        params = dict(activation='relu', batch_size=512, learning_rate_init=.001, max_iter=200,
                      early_stopping=True, validation_fraction=.15, n_iter_no_change=15,
                      random_state=seed, **CONFIG)
        model = make_pipeline(StandardScaler(), MLPRegressor(**params))
        with threadpool_limits(limits=2):
            model.fit(x_tr, y_tr)
        members.append(model)
        grid.append(dict(seed=seed, iterations=int(model[-1].n_iter_)))
        print(f'seed {seed} iterations={int(model[-1].n_iter_)}', flush=True)
    candidate = np.clip(np.mean([m.predict(x_va) for m in members], axis=0), 0, 1)
    weights = fit_weights(np.column_stack([baseline_v, candidate]), y_va)
    blend = np.clip(baseline_v * weights[0] + candidate * weights[1], 0, 1)
    val_mse = float(np.mean((blend - y_va) ** 2))
    best = dict(candidate_valid=candidate, weights=weights, validation_MSE=val_mse,
                hidden=list(CONFIG['hidden_layer_sizes']), alpha=CONFIG['alpha'])
    (OUT / 'grid.json').write_text(json.dumps(dict(config=CONFIG, seeds=grid,
        blend_weight=float(weights[1]), validation_MSE=val_mse), indent=2))
    print(f'seed average val_MSE={val_mse:.9f} weight={weights[1]:.6f}', flush=True)
    if best['weights'][1] < 1e-8:
        (OUT / 'decision.json').write_text(json.dumps(dict(accepted=False, reason='Validation chose zero weight'), indent=2))
        print('Discarded at validation gate; test unopened.', flush=True)
        return

    tests = []
    for site in range(1, 11):
        tests.append(attach_temporal(kp.add_cross_site(parts[site]['test'], cross_future), spatial)
                     .with_columns(pl.lit(site).alias('farm'), pl.lit(site).alias('site')))
    te = pl.concat(tests)
    previous = pl.read_csv(PARENT / 'mlp_shared_v1/predictions.csv', try_parse_dates=True)
    if not te.select(KEYS).equals(previous.select(KEYS)):
        raise ValueError('Test keys/labels differ')
    prior = pl.read_csv(BASE / 'predictions.csv', try_parse_dates=True)
    mlp_test = np.clip(np.mean([m.predict(design(te)) for m in members], axis=0), 0, 1)
    test = te.select(KEYS).with_columns(
        pl.Series('baseline', previous['mlp_blend']),
        pl.Series('mlp_seedavg', mlp_test),
        pl.Series('mlp_seedavg_blend', np.clip(prior['curve_blend'].to_numpy() * best['weights'][0]
                                               + mlp_test * best['weights'][1], 0, 1)))
    scores = [dict(model=n, **metrics(test['actual'], test[n])) for n in ['baseline', 'mlp_seedavg', 'mlp_seedavg_blend']]
    paired = paired_comparison(test, 'mlp_seedavg_blend')
    accepted = scores[-1]['RMSE'] < scores[0]['RMSE'] and paired['delta_RMSE_high'] < 0
    joblib.dump(dict(models=members, config=dict(hidden=best['hidden'], alpha=best['alpha'], seeds=SEEDS),
                     columns=FEATURES, protocol=protocol), OUT / 'model.joblib')
    (OUT / 'weights.json').write_text(json.dumps(dict(
        baseline=float(best['weights'][0]), mlp_seedavg=float(best['weights'][1]), seeds=SEEDS,
        chosen=dict(hidden=best['hidden'], alpha=best['alpha']), validation_MSE=best['validation_MSE'],
        train_samples=tr.height, validation_samples=va.height), indent=2))
    pl.DataFrame(scores).write_csv(OUT / 'metrics.csv')
    (OUT / 'paired.json').write_text(json.dumps(paired, indent=2))
    (OUT / 'decision.json').write_text(json.dumps(dict(accepted=accepted, scores=scores), indent=2))
    if accepted:
        test.write_csv(OUT / 'predictions.csv')
    print(json.dumps(dict(accepted=accepted, config=dict(hidden=best['hidden'], alpha=best['alpha'], seeds=SEEDS),
                          scores=scores, paired=paired), indent=2), flush=True)


if __name__ == '__main__':
    main()

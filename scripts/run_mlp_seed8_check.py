"""Validation-only probe: does an eight-seed average clear the -0.0002 gate?

Training stays on the 2012 OOF panel; validation fits the convex weight and is the
only place the gate is measured. The test set is opened only if the gate passes.
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
OUT = PARENT / 'mlp_seed8_check_v1'
KEYS = ['origin', 'valid_at', 'lead', 'actual', 'site']
FEATURES = kp.STAGE2 + COLUMNS + TEMPORAL_COLUMNS
NEW_SEEDS = [555, 777, 888, 999, 1234]
CONFIG = dict(hidden_layer_sizes=(128, 64, 32), alpha=0.1)
GATE = -0.0002


def design(frame, columns):
    farms = frame['farm'].to_numpy().astype(int)
    return np.column_stack([frame.select(columns).to_numpy(), np.eye(10)[farms - 1]])


def main():
    registry = json.loads(Path('docs/归档/gefcom2014/optimization-progress.json').read_text())
    if registry['milestones'][-1]['id'] != 'mlp_seedavg_tuned':
        raise ValueError('Baseline changed; refusing to run')
    OUT.mkdir(exist_ok=False)
    cfg = config()
    bundle = joblib.load(PARENT / 'mlp_seedavg_tuned_v1/model.joblib')
    existing = bundle['models']
    columns = bundle['columns']
    w_m = json.loads((PARENT / 'mlp_seedavg_tuned_v1/weights.json').read_text())
    protocol = dict(
        method='Eight-seed average of the selected MLP architecture (validation probe only)',
        new_seeds=NEW_SEEDS, config=CONFIG, features=columns,
        training_data='2012 OOF panel only', early_stopping='inside training rows only',
        blend_weights='2013 H1 validation', gate=f'validation CI upper <= {GATE}',
        test_scoring='only if the gate passes, single pass')
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
    t_panel = temporal_spatial_panel(panels)
    cross_train, cross_future = kp.combine(oofs), kp.combine(crosses)

    trains, validations, tests = [], [], []
    for site in range(1, 11):
        tr = parts[site]['train'].join(cross_train, on=['origin', 'valid_at'], how='inner', validate='1:1') \
            .sort('origin', 'valid_at')
        expected = pl.read_csv(f'artifacts/kpower_reproduction/{ID}_full/site_{site}/train_keys.csv', try_parse_dates=True)
        if not tr.select('origin', 'valid_at').equals(expected):
            raise ValueError('Training keys differ')
        trains.append(attach_temporal(tr, t_panel).with_columns(pl.lit(site).alias('farm')))
        validations.append(attach_temporal(kp.add_cross_site(parts[site]['validation'], cross_future), t_panel)
                           .with_columns(pl.lit(site).alias('farm'), pl.lit(site).alias('site')))
        tests.append(attach_temporal(kp.add_cross_site(parts[site]['test'], cross_future), t_panel)
                     .with_columns(pl.lit(site).alias('farm'), pl.lit(site).alias('site')))
    tr, va, te = pl.concat(trains), pl.concat(validations), pl.concat(tests)
    x_tr, y_tr = design(tr, columns), tr['actual'].to_numpy()
    x_va = design(va, columns)

    members = list(existing)
    for seed in NEW_SEEDS:
        params = dict(activation='relu', batch_size=512, learning_rate_init=.001, max_iter=200,
                      early_stopping=True, validation_fraction=.15, n_iter_no_change=15,
                      random_state=seed, **CONFIG)
        model = make_pipeline(StandardScaler(), MLPRegressor(**params))
        with threadpool_limits(limits=2):
            model.fit(x_tr, y_tr)
        members.append(model)
        print(f'seed {seed} iterations={int(model[-1].n_iter_)}', flush=True)
    ens = np.clip(np.mean([m.predict(x_va) for m in members], axis=0), 0, 1)

    curve_v = pl.read_csv(PARENT / 'curve_residual_v1/validation_predictions.csv', try_parse_dates=True) \
        .select(['origin', 'valid_at', 'site', 'curve_blend'])
    mlp3_v = np.clip(np.mean([m.predict(x_va) for m in existing], axis=0), 0, 1)
    frame = va.select(KEYS).with_columns(pl.Series('curve_blend', curve_v['curve_blend']),
                                         pl.Series('mlp3', mlp3_v), pl.Series('ens8', ens))
    frame = frame.with_columns(
        (pl.col('curve_blend') * w_m['baseline'] + pl.col('mlp3') * w_m['mlp_seedavg']).alias('baseline'))
    w = fit_weights(np.column_stack([frame['curve_blend'].to_numpy(), frame['ens8'].to_numpy()]),
                    frame['actual'].to_numpy())
    frame = frame.with_columns(pl.Series(
        'candidate', np.clip(frame['curve_blend'].to_numpy() * w[0] + frame['ens8'].to_numpy() * w[1], 0, 1)))
    scores = [dict(model=n, **metrics(frame['actual'], frame[n])) for n in ('baseline', 'candidate')]
    paired = paired_comparison(frame, 'candidate')
    gate = scores[1]['RMSE'] < scores[0]['RMSE'] and paired['delta_RMSE_high'] <= GATE
    (OUT / 'validation_gate.json').write_text(json.dumps(dict(
        weight=float(w[1]), scores=scores, paired=paired, gate=GATE, passed=bool(gate)), indent=2))
    print(json.dumps(dict(val_baseline=scores[0]['RMSE'], val_candidate=scores[1]['RMSE'],
                          delta=paired['delta_RMSE'], ci=[paired['delta_RMSE_low'], paired['delta_RMSE_high']],
                          gate_passed=bool(gate)), indent=2), flush=True)
    if not gate:
        (OUT / 'decision.json').write_text(json.dumps(dict(accepted=False, reason='validation gate failed'), indent=2))
        print('Gate failed; test unopened.', flush=True)
        return

    x_te = design(te, columns)
    ens_te = np.clip(np.mean([m.predict(x_te) for m in members], axis=0), 0, 1)
    previous = pl.read_csv(PARENT / 'mlp_seedavg_tuned_v1/predictions.csv', try_parse_dates=True)
    test = te.select(KEYS).with_columns(pl.Series('ens8', ens_te)) \
        .join(previous.select(KEYS + ['mlp_seedavg_blend']), on=KEYS, how='inner', validate='1:1')
    if test.height != 42432:
        raise ValueError(f'Test keys differ: {test.height}')
    curve_t = pl.read_csv(PARENT / 'curve_residual_v1/predictions.csv', try_parse_dates=True) \
        .select(KEYS + ['curve_blend'])
    test = test.join(curve_t, on=KEYS, how='inner', validate='1:1')
    final = np.clip(test['curve_blend'].to_numpy() * w[0] + test['ens8'].to_numpy() * w[1], 0, 1)
    result = test.select(KEYS).with_columns(pl.Series('baseline', test['mlp_seedavg_blend']),
                                            pl.Series('seed8_blend', final))
    scores = [dict(model=n, **metrics(result['actual'], result[n])) for n in ('baseline', 'seed8_blend')]
    paired = paired_comparison(result, 'seed8_blend')
    accepted = scores[-1]['RMSE'] < scores[0]['RMSE'] and paired['delta_RMSE_high'] < 0
    joblib.dump(dict(models=members, columns=columns, protocol=protocol), OUT / 'model.joblib')
    pl.DataFrame(scores).write_csv(OUT / 'metrics.csv')
    (OUT / 'paired.json').write_text(json.dumps(paired, indent=2))
    (OUT / 'decision.json').write_text(json.dumps(dict(accepted=accepted, scores=scores), indent=2))
    if accepted:
        result.write_csv(OUT / 'predictions.csv')
    print(json.dumps(dict(accepted=accepted, scores=scores, paired=paired), indent=2), flush=True)


if __name__ == '__main__':
    main()

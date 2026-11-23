"""Frozen-weight replacement of the temporal-NWP branch after final past-data refit.

Hypothesis: the ten per-site temporal-NWP stage-two models are data-starved
relative to the shared branch (each sees ~6.6k rows vs 65.8k). Refitting them on
the extended OOF period (through 2013-06-30) with their original early-stopping
rounds frozen should transfer the same data benefit that the shared and MLP
branches already showed.
"""
from datetime import datetime
import json
from pathlib import Path

import joblib
import numpy as np
import polars as pl
from lightgbm import LGBMRegressor

from run_shared_final_refit import past_oof
from windpower.features.build import build
from windpower.features.cross_nwp import temporal_spatial_panel, attach_temporal, COLUMNS, TEMPORAL_COLUMNS
from windpower.models.train import config, partitions
from windpower.models import kpower_reproduction as kp
from windpower.evaluation.metrics import metrics
from windpower.evaluation.paired import paired_comparison

ID = 'ablation_20261114_v1'
PARENT = Path('artifacts/kpower_ablation') / ID
OUT = PARENT / 'temporal_final_refit_v1'
KEYS = ['origin', 'valid_at', 'lead', 'actual', 'site']
FEATURES = kp.STAGE2 + COLUMNS + TEMPORAL_COLUMNS


def main():
    registry = json.loads(Path('docs/归档/gefcom2014/optimization-progress.json').read_text())
    if registry['milestones'][-1]['id'] != 'mlp_final_refit':
        raise ValueError('Baseline changed; refusing to run')
    OUT.mkdir(exist_ok=False)
    cfg = config()
    cutoff = datetime.fromisoformat(cfg['splits']['test'][0])

    refit = joblib.load(PARENT / 'shared_final_refit_v1/model.joblib')
    stage1 = refit['stage1']
    old = {s: joblib.load(PARENT / 'temporal_nwp_v1' / f'site_{s}.joblib')['models'] for s in range(1, 11)}
    rounds = {s: old[s][0].best_iteration_ for s in range(1, 11)}
    w_t = json.loads((PARENT / 'temporal_nwp_v1/weights.json').read_text())
    w_s = json.loads((PARENT / 'shared_model_v1/weights.json').read_text())
    w_c = json.loads((PARENT / 'curve_residual_v1/weights.json').read_text())
    w_m = json.loads((PARENT / 'mlp_shared_v1/weights.json').read_text())
    coefficient = w_t['temporal'] * w_s['baseline'] * w_c['baseline'] * w_m['baseline']

    protocol = dict(
        method='Replace only the accepted temporal-NWP branch after final train+validation refit',
        cutoff=str(cutoff), label_rule='All final fitting labels strictly before first test origin',
        frozen_rounds=rounds, fixed_branch_coefficient=coefficient,
        selection='No new model/round/weight selection; original selected rounds and combination coefficients frozen',
        protocol_change='Former validation labels become final fitting data; no independent validation remains for this refit',
        caveat='Exploratory fixed historical test; NWP issue and observation arrival timestamps unknown')
    (OUT / 'protocol.json').write_text(json.dumps(protocol, indent=2))

    panels, parts, extended, stage1_predictions, oofs, audit = {}, {}, {}, {}, [], []
    for site in range(1, 11):
        panel = kp.nwp_panel(site)
        panels[site] = panel
        parts[site] = {k: kp.attach(v, panel) for k, v in partitions(build(site)).items()}
        extended[site] = pl.concat([parts[site]['train'], parts[site]['validation']]) \
            .filter(pl.col('valid_at') < cutoff).sort('origin', 'valid_at')
        stage1_predictions[site] = panel.select('origin', 'valid_at').with_columns(
            pl.Series(f'stage1_{site}', np.clip(stage1[site].predict(panel.select(kp.STAGE1).to_numpy()), 0, 1)))
        oof, proof = past_oof(extended[site], panel, cfg['lightgbm'], site)
        oofs.append(oof)
        audit.extend(proof)
        print(f'Final-refit first stage zone {site}', flush=True)
    (OUT / 'oof_audit.json').write_text(json.dumps(audit, indent=2))

    spatial = temporal_spatial_panel(panels)
    train_cross, test_cross = kp.combine(oofs), kp.combine([stage1_predictions[s] for s in range(1, 11)])

    models, validations, tests = {}, [], []
    for site in range(1, 11):
        tr = extended[site].join(train_cross, on=['origin', 'valid_at'], how='inner', validate='1:1') \
            .sort('origin', 'valid_at')
        tr = attach_temporal(tr, spatial)
        if tr['valid_at'].max() >= cutoff:
            raise ValueError('Final training crosses test origin')
        params = dict(cfg['lightgbm'], n_estimators=int(rounds[site]), random_state=42)
        model = LGBMRegressor(**params)
        model.fit(tr.select(FEATURES).to_numpy(), tr['actual'].to_numpy())
        models[site] = model
        te = attach_temporal(kp.add_cross_site(parts[site]['test'], test_cross), spatial) \
            .with_columns(pl.lit(site).alias('site'))
        tests.append(te.select(KEYS).with_columns(
            pl.Series('temporal', np.clip(model.predict(te.select(FEATURES).to_numpy()), 0, 1))))
        print(f'Refit temporal zone {site} rounds={rounds[site]} samples={tr.height}', flush=True)
    train_last = max(extended[s]['valid_at'].max() for s in range(1, 11))
    joblib.dump(dict(models=models, columns=FEATURES, protocol=protocol), OUT / 'model.joblib')
    (OUT / 'weights.json').write_text(json.dumps(dict(
        temporal_branch=coefficient, frozen_rounds=rounds,
        train_samples={s: int(extended[s].height) for s in range(1, 11)},
        train_last=str(train_last)), indent=2))

    candidate = pl.concat(tests)
    previous = pl.read_csv(PARENT / 'mlp_final_refit_v1/predictions.csv', try_parse_dates=True)
    old_temporal = pl.read_csv(PARENT / 'temporal_nwp_v1/predictions.csv', try_parse_dates=True)
    joined = previous.join(candidate, on=KEYS, how='inner', validate='1:1')
    joined = joined.join(old_temporal.select(KEYS + ['temporal']).rename({'temporal': 'old_temporal'}),
                         on=KEYS, how='inner', validate='1:1')
    if joined.height != previous.height or joined.height != old_temporal.height or joined.height != len(KEYS) * 0 + 42432:
        raise ValueError(f'Test keys/labels differ: {joined.height}')

    replacement = joined['temporal'].to_numpy()
    retained = joined['mlp_final_refit'].to_numpy() - coefficient * joined['old_temporal'].to_numpy()
    if (retained < -1e-12).any() or (retained > 1 - coefficient + 1e-12).any():
        raise ValueError('Invalid branch decomposition')
    result = joined.select(KEYS).with_columns(
        pl.Series('baseline', joined['mlp_final_refit']),
        pl.Series('refit_temporal', replacement),
        pl.Series('final_refit', np.clip(retained + coefficient * replacement, 0, 1)))
    result = result.with_columns(((pl.col('final_refit') - pl.col('actual')) ** 2).alias('se'))
    site_metrics = result.group_by('site').agg(
        pl.len().alias('n'),
        (pl.col('se').mean()).sqrt().alias('RMSE'),
        (pl.col('final_refit') - pl.col('actual')).abs().mean().alias('MAE')).sort('site')
    scores = [dict(model=n, **metrics(result['actual'], result[n])) for n in ['baseline', 'refit_temporal', 'final_refit']]
    paired = paired_comparison(result, 'final_refit')
    accepted = scores[-1]['RMSE'] < scores[0]['RMSE'] and paired['delta_RMSE_high'] < 0
    verification = dict(
        test_hours=result.height,
        frozen_branch_replacement_verified=bool(coefficient > 0),
        all_oof_labels_strictly_past=all(a['train_max'] < a['origin_min'] for a in audit),
        final_labels_before_test_origin=bool(train_last < cutoff),
        final_train_last=str(train_last),
        frozen_rounds=rounds,
        no_independent_validation_predictions_published=True)
    pl.DataFrame(scores).write_csv(OUT / 'metrics.csv')
    site_metrics.write_csv(OUT / 'site_metrics.csv')
    (OUT / 'paired.json').write_text(json.dumps(paired, indent=2))
    (OUT / 'verification.json').write_text(json.dumps(verification, indent=2))
    (OUT / 'decision.json').write_text(json.dumps(dict(accepted=accepted, scores=scores), indent=2))
    if accepted:
        result.select(KEYS + ['baseline', 'refit_temporal', 'final_refit']).write_csv(OUT / 'predictions.csv')
    print(json.dumps(dict(accepted=accepted, scores=scores, paired=paired), indent=2), flush=True)


if __name__ == '__main__':
    main()

"""Reference component distributions on validation and test, compared with the 2012 OOF panel.

Reads only saved component predictions; labels are used for grouping, never for fitting.
"""
import json
from pathlib import Path

import numpy as np
import polars as pl

ID = 'ablation_20261114_v1'
PARENT = Path('artifacts/kpower_ablation') / ID
OOF_DIR = PARENT / 'meta_oof_feasibility_v1'
KEYS = ['origin', 'valid_at', 'lead', 'actual', 'site']
COMPS = ['C', 'neighbors', 'full', 'spatial', 'temporal', 'shared', 'curve', 'mlp']


def load_validation():
    base = None
    for comp in ('C', 'neighbors', 'full'):
        frames = []
        for site in range(1, 11):
            path = Path(f'artifacts/kpower_reproduction/{ID}_{comp}/site_{site}/validation_predictions.csv')
            frames.append(pl.read_csv(path, try_parse_dates=True)
                          .select(['origin', 'valid_at', 'prediction'])
                          .with_columns(pl.lit(site).alias('site')).rename({'prediction': comp}))
        block = pl.concat(frames)
        base = block if base is None else base.join(block, on=['origin', 'valid_at', 'site'],
                                                    how='inner', validate='1:1')
    for comp, folder, column in (('temporal', 'temporal_nwp_v1', 'temporal'),
                                 ('shared', 'shared_model_v1', 'shared'),
                                 ('curve', 'curve_residual_v1', 'curve_residual'),
                                 ('mlp', 'mlp_shared_v1', 'mlp')):
        frame = pl.read_csv(PARENT / folder / 'validation_predictions.csv', try_parse_dates=True) \
            .select(['origin', 'valid_at', 'site', 'lead', 'actual', column]).rename({column: comp})
        base = base.join(frame, on=['origin', 'valid_at', 'site'], how='inner', validate='1:1',
                         suffix=f'_{comp}')
    return base


def load_test():
    comps = pl.read_csv(PARENT / 'paired_predictions.csv', try_parse_dates=True) \
        .select(KEYS + ['C', 'neighbors', 'full'])
    for comp, folder, column in (('spatial', 'spatial_nwp_v1', 'spatial'),
                                 ('temporal', 'temporal_nwp_v1', 'temporal'),
                                 ('shared', 'shared_model_v1', 'shared'),
                                 ('curve', 'curve_residual_v1', 'curve_residual'),
                                 ('mlp', 'mlp_seedavg_tuned_v1', 'mlp_seedavg')):
        frame = pl.read_csv(PARENT / folder / 'predictions.csv', try_parse_dates=True) \
            .select(KEYS).with_columns(pl.read_csv(PARENT / folder / 'predictions.csv', try_parse_dates=True)[column].alias(comp))
        comps = comps.join(frame, on=KEYS, how='inner', validate='1:1')
    return comps


def profile(frame, label, components):
    out = dict(label=label, rows=frame.height, components={})
    for comp in components:
        col = frame[comp].to_numpy()
        out['components'][comp] = dict(mean=float(np.mean(col)), std=float(np.std(col)))
    for bucket, col in (('lead', pl.col('lead') // 8), ('power', (pl.col('actual') * 5).floor().clip(0, 4))):
        grouped = frame.with_columns(col.cast(pl.Int64).alias('bucket')).group_by('bucket').agg(
            [pl.len().alias('n')] + [pl.col(c).mean().alias(c) for c in components]).sort('bucket')
        out[bucket] = {r['bucket']: {c: r[c] for c in components} | {'n': r['n']}
                       for r in grouped.to_dicts()}
    out['site'] = {r['site']: {c: r[c] for c in components}
                   for r in frame.group_by('site').agg([pl.col(c).mean().alias(c) for c in components]).to_dicts()}
    return out


def main():
    oof = pl.read_csv(OOF_DIR / 'oof_components.csv', try_parse_dates=True)
    val = load_validation()
    test = load_test()
    common = [c for c in COMPS if c in val.columns and c in test.columns]
    report = dict(
        note=('validation lacks the spatial branch and uses the earlier single MLP as the MLP reference; '
              'test uses the deployed branches'),
        components=common,
        oof=profile(oof, 'oof', [c for c in common if c in oof.columns]),
        validation=profile(val, 'validation', common),
        test=profile(test, 'test', common))
    shifts = {}
    for comp in common:
        a, b, c = report['oof']['components'][comp], report['validation']['components'][comp], report['test']['components'][comp]
        shifts[comp] = dict(oof_mean=a['mean'], val_mean=b['mean'], test_mean=c['mean'],
                            oof_std=a['std'], val_std=b['std'], test_std=c['std'],
                            abs_mean_gap_val=abs(a['mean'] - b['mean']), abs_mean_gap_test=abs(a['mean'] - c['mean']))
    report['shift_summary'] = shifts
    (OOF_DIR / 'reference_distributions.json').write_text(json.dumps(report, indent=2))
    with pl.Config(tbl_rows=20):
        print(pl.DataFrame([dict(component=k, **v) for k, v in shifts.items()]).select(
            'component', 'oof_mean', 'val_mean', 'test_mean', 'oof_std', 'test_std',
            'abs_mean_gap_val', 'abs_mean_gap_test'))
    print(json.dumps(dict(rows=dict(oof=oof.height, validation=val.height, test=test.height)), indent=2))


if __name__ == '__main__':
    main()

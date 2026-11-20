"""Locate which component drifts in the serving replay vs the frozen chain."""
import json
from pathlib import Path

import polars as pl

from windpower.serving.predict import SITES, infer

ABL = Path('artifacts/kpower_ablation/ablation_20261114_v1')
REPRO = Path('artifacts/kpower_reproduction')


def main() -> None:
    frozen = pl.read_csv(ABL / 'mlp_seedavg_tuned_v1/predictions.csv', try_parse_dates=True)
    origins = frozen['origin'].unique().to_list()
    result = infer(sites=SITES, origins=origins, dry_run=True)
    serving = result.with_columns(pl.col('farm').alias('site'))

    # Per-component frozen test predictions from each accepted round.
    sources = {}
    for name in ('C', 'neighbors', 'full'):
        frames = []
        for s in SITES:
            f = pl.read_csv(REPRO / f'ablation_20261114_v1_{name}/site_{s}/predictions.csv',
                            try_parse_dates=True)
            frames.append(f.select('origin', 'valid_at', 'lead',
                                   pl.lit(s).alias('site'), pl.col('prediction').alias(name)))
        sources[name] = pl.concat(frames)
    wide = None
    for name, frame in sources.items():
        wide = frame if wide is None else wide.join(frame, on=['site', 'origin', 'valid_at', 'lead'],
                                                    how='inner', validate='1:1')
    frozen_paths = {'spatial': ABL / 'spatial_nwp_v1/predictions.csv',
                    'temporal': ABL / 'temporal_nwp_v1/predictions.csv',
                    'shared': ABL / 'shared_model_v1/predictions.csv',
                    'curve': ABL / 'curve_residual_v1/predictions.csv',
                    'mlp': ABL / 'mlp_seedavg_tuned_v1/predictions.csv'}
    columns = {'spatial': 'spatial', 'temporal': 'temporal', 'shared': 'shared',
               'curve': 'curve_residual', 'mlp': 'mlp_seedavg'}
    for name, path in frozen_paths.items():
        f = pl.read_csv(path, try_parse_dates=True)
        f = f.select('site', 'origin', 'valid_at', 'lead',
                     pl.col(columns[name]).alias(name))
        wide = wide.join(f, on=['site', 'origin', 'valid_at', 'lead'],
                         how='inner', validate='1:1')

    components = ['C', 'curve', 'full', 'mlp', 'neighbors', 'shared', 'spatial', 'temporal']
    joined = wide.join(serving.select('site', 'origin', 'valid_at', 'lead', *components),
                       on=['site', 'origin', 'valid_at', 'lead'],
                       how='inner', validate='1:1', suffix='_serving')
    report = {}
    for name in ('C', 'neighbors', 'full', 'spatial', 'temporal', 'shared', 'curve', 'mlp'):
        diff = (joined[name] - joined[f'{name}_serving']).abs()
        worst = joined.filter(diff > 1e-9)
        report[name] = dict(drifted_rows=worst.height,
                             max_diff=float(diff.max()))
        if worst.height:
            report[name].update(
                sites=worst['site'].unique().sort().to_list(),
                first_origin=str(worst['origin'].min()), last_origin=str(worst['origin'].max()))
    print(json.dumps(report, indent=2, default=str))


if __name__ == '__main__':
    main()

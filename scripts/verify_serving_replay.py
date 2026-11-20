"""Verify the P2 serving entry reproduces the frozen optimization-chain test replay.

Engineering equivalence only: the test period is already scored and frozen;
this script re-derives the same rows through the serving path and fails loudly
on any drift. No selection or tuning happens here.
"""
import json
from pathlib import Path

import numpy as np
import polars as pl

from windpower.evaluation.metrics import pooled_metrics
from windpower.serving.predict import SITES, infer

FROZEN = Path('artifacts/kpower_ablation/ablation_20261114_v1'
              '/mlp_seedavg_tuned_v1/predictions.csv')
TOLERANCE = 1e-9


def main() -> None:
    frozen = pl.read_csv(FROZEN, try_parse_dates=True)
    origins = frozen['origin'].unique().to_list()
    result = infer(sites=SITES, origins=origins, dry_run=True)

    serving = result.select(
        pl.col('farm').alias('site'), 'origin', 'valid_at', 'lead', 'prediction')
    frozen_keys = frozen.select('site', 'origin', 'valid_at', 'lead', 'actual',
                                pl.col('mlp_seedavg_blend').alias('frozen'))
    joined = frozen_keys.join(serving, on=['site', 'origin', 'valid_at', 'lead'],
                             how='inner', validate='1:1').sort('site', 'origin', 'valid_at')
    if joined.height != frozen.height:
        raise SystemExit(f'row mismatch: frozen {frozen.height} vs joined {joined.height}')
    delta = (joined['prediction'] - joined['frozen']).to_numpy()
    diff_max = float(np.abs(delta).max())
    serving_p = joined['prediction'].to_numpy()
    frozen_p = joined['frozen'].to_numpy()
    actual = joined['actual'].to_numpy()
    rmse = float(np.sqrt(np.mean((serving_p - actual) ** 2)))
    frozen_rmse = float(np.sqrt(np.mean((frozen_p - actual) ** 2)))
    report = dict(rows=joined.height, max_abs_diff=diff_max,
                  serving_RMSE=rmse, frozen_RMSE=frozen_rmse,
                  tolerance=TOLERANCE, passed=diff_max <= TOLERANCE)
    print(json.dumps(report, indent=2))
    if not report['passed']:
        raise SystemExit('serving replay drifted from the frozen chain')


if __name__ == '__main__':
    main()

"""Validation-only temporal gate for sitewise weights shrunk toward global blend."""
import argparse
from datetime import datetime
import json
from pathlib import Path

import numpy as np
import polars as pl

from run_validation_blend import load, NAMES
from windpower.evaluation.metrics import metrics
from windpower.evaluation.paired import paired_comparison
from windpower.models.validation_blend import fit_weights


def fit(frame):
    global_weights = fit_weights(frame.select(NAMES).to_numpy(), frame['actual'].to_numpy())
    site_weights = {site: fit_weights(
        frame.filter(pl.col('site') == site).select(NAMES).to_numpy(),
        frame.filter(pl.col('site') == site)['actual'].to_numpy()) for site in range(1, 11)}
    return global_weights, site_weights


def predict(frame, global_weights, site_weights, strength):
    output = np.empty(frame.height)
    for site in range(1, 11):
        mask = frame['site'].to_numpy() == site
        weights = (1-strength)*global_weights + strength*site_weights[site]
        output[mask] = frame.filter(pl.col('site') == site).select(NAMES).to_numpy() @ weights
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ablation-id', required=True)
    args = parser.parse_args()
    if not args.ablation_id.replace('_', '').replace('-', '').isalnum():
        parser.error('Invalid ablation ID')
    parent = Path('artifacts/kpower_ablation') / args.ablation_id
    out = parent / 'site_blend_v1'
    out.mkdir(exist_ok=False)
    folders = {name: Path('artifacts/kpower_reproduction') / f'{args.ablation_id}_{name}' for name in NAMES}
    # Predeclared grid and temporal split; test files are not opened until frozen.
    grid = [0., .25, .5, .75, 1.]
    validation = load(folders, 'validation_predictions.csv')
    early = validation.filter(pl.col('valid_at') <= datetime(2013, 4, 1))
    late = validation.filter(pl.col('valid_at') > datetime(2013, 4, 1))
    gw, sw = fit(early)
    candidates = [dict(strength=a, **metrics(late['actual'], predict(late, gw, sw, a))) for a in grid]
    selected = min(candidates, key=lambda row: (row['RMSE'], row['strength']))['strength']
    spec = dict(grid=grid, temporal_split='Jan-Mar weights -> Apr-Jun selection; then refit Jan-Jun',
                candidates=candidates, selected_strength=selected,
                caveat='Base models already use full validation for early stopping; historical benchmark')
    (out / 'selection.json').write_text(json.dumps(spec, indent=2))
    if selected == 0:
        print('Temporal validation selected global blend; discard sitewise candidate.')
        return
    gw, sw = fit(validation)
    spec.update(global_weights=gw.tolist(), site_weights={str(k):v.tolist() for k,v in sw.items()})
    (out / 'weights.json').write_text(json.dumps(spec, indent=2))
    test = load(folders, 'predictions.csv')
    old_weights = json.loads((parent/'validation_blend/weights.json').read_text())['weights']
    test = test.with_columns(
        pl.Series('baseline', test.select(NAMES).to_numpy() @ np.array([old_weights[n] for n in NAMES])),
        pl.Series('site_blend', predict(test, gw, sw, selected)))
    scores = [dict(model=name, **metrics(test['actual'],test[name])) for name in ['baseline','site_blend']]
    paired = paired_comparison(test, 'site_blend')
    accepted = scores[1]['RMSE'] < scores[0]['RMSE'] and paired['delta_RMSE_high'] < 0
    pl.DataFrame(scores).write_csv(out/'metrics.csv')
    (out/'paired.json').write_text(json.dumps(paired,indent=2))
    (out/'decision.json').write_text(json.dumps(dict(accepted=accepted,scores=scores),indent=2))
    if accepted:
        test.write_csv(out/'predictions.csv')
    print(json.dumps(dict(accepted=accepted, selected_strength=selected, scores=scores, paired=paired),indent=2))


if __name__ == '__main__':
    main()

"""Joint validation-only simplex fit across the seven previously accepted components."""
import json
from pathlib import Path

import numpy as np
import polars as pl

from run_validation_blend import load
from windpower.models.validation_blend import fit_weights
from windpower.evaluation.metrics import metrics
from windpower.evaluation.paired import paired_comparison

ID='ablation_20261114_v1'
PARENT=Path('artifacts/kpower_ablation')/ID
OUT=PARENT/'joint_blend_v1'
BASE=PARENT/'curve_residual_v1'
NAMES=['C','neighbors','full','spatial','temporal','shared','curve_residual']
KEYS=['origin','valid_at','lead','actual','site']


def read_matching(folder,filename,reference):
    frame=pl.read_csv(folder/filename,try_parse_dates=True)
    if not frame.select(KEYS).equals(reference.select(KEYS)):
        raise ValueError(f'Samples differ in {folder.name}/{filename}')
    return frame


def components(filename):
    original=load({n:Path(f'artifacts/kpower_reproduction/{ID}_{n}') for n in NAMES[:3]},filename)
    temporal=read_matching(PARENT/'temporal_nwp_v1',filename,original)
    shared=read_matching(PARENT/'shared_model_v1',filename,original)
    curve=read_matching(BASE,filename,original)
    if filename=='validation_predictions.csv':
        # The older spatial run saved its blend inside the next run's baseline.
        # Invert only known frozen prediction weights, never labels.
        first=json.loads((PARENT/'validation_blend/weights.json').read_text())['weights']
        spatial_weights=json.loads((PARENT/'spatial_nwp_v1/weights.json').read_text())
        original_blend=original.select(NAMES[:3]).to_numpy()@np.array([first[n] for n in NAMES[:3]])
        spatial=(temporal['baseline'].to_numpy()-spatial_weights['baseline']*original_blend)/spatial_weights['spatial']
        if ((spatial < -1e-12)|(spatial>1+1e-12)).any():
            raise ValueError('Invalid recovered spatial validation predictions')
        spatial=np.clip(spatial,0,1)
    else:
        spatial=read_matching(PARENT/'spatial_nwp_v1',filename,original)['spatial'].to_numpy()
    return original.with_columns(pl.Series('spatial',spatial),pl.Series('temporal',temporal['temporal']),
        pl.Series('shared',shared['shared']),pl.Series('curve_residual',curve['curve_residual']),
        pl.Series('baseline',curve['curve_blend']))


def main():
    registry=json.loads(Path('docs/研究记录/optimization-progress.json').read_text())
    if registry['milestones'][-1]['id']!='curve_residual_blend':
        raise ValueError('Current best changed; declare a new baseline/components protocol')
    OUT.mkdir(exist_ok=False)
    (OUT/'protocol.json').write_text(json.dumps(dict(components=NAMES,baseline=str(BASE),
        method='One simultaneous nonnegative sum-to-one validation MSE fit across accepted components',
        caveat='Same historical test; validation reused by base-model stopping'),indent=2))
    va=components('validation_predictions.csv')
    weights=fit_weights(va.select(NAMES).to_numpy(),va['actual'].to_numpy())
    (OUT/'weights.json').write_text(json.dumps(dict(weights=dict(zip(NAMES,weights.tolist())),validation_samples=va.height),indent=2))
    # Test sources are opened only after joint weights are frozen.
    te=components('predictions.csv')
    te=te.with_columns(pl.Series('joint_blend',te.select(NAMES).to_numpy()@weights))
    scores=[dict(model=n,**metrics(te['actual'],te[n])) for n in ['baseline','joint_blend']]
    paired=paired_comparison(te,'joint_blend')
    accepted=scores[1]['RMSE']<scores[0]['RMSE'] and paired['delta_RMSE_high']<0
    pl.DataFrame(scores).write_csv(OUT/'metrics.csv')
    (OUT/'paired.json').write_text(json.dumps(paired,indent=2))
    (OUT/'decision.json').write_text(json.dumps(dict(accepted=accepted,scores=scores),indent=2))
    if accepted:
        te.write_csv(OUT/'predictions.csv')
        va.with_columns(pl.Series('joint_blend',va.select(NAMES).to_numpy()@weights)).write_csv(OUT/'validation_predictions.csv')
    print(json.dumps(dict(accepted=accepted,weights=dict(zip(NAMES,weights.tolist())),scores=scores,paired=paired),indent=2))


if __name__=='__main__':main()

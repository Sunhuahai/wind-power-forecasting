"""Temporally gated horizon-specific weights for the two accepted branches."""
from datetime import datetime
import json
from pathlib import Path
import numpy as np
import polars as pl
from windpower.models.lead_blend import fit_lead_weights,predict_lead_blend
from windpower.evaluation.metrics import metrics
from windpower.evaluation.paired import paired_comparison

PARENT=Path('artifacts/kpower_ablation/ablation_20261114_v1')
BASE=PARENT/'mlp_shared_v1'
OUT=PARENT/'lead_blend_v1'
KEYS=['origin','valid_at','lead','actual','site']


def main():
    OUT.mkdir(exist_ok=False)
    va=pl.read_csv(BASE/'validation_predictions.csv',try_parse_dates=True)
    early=va.filter(pl.col('valid_at')<=datetime(2013,4,1))
    late=va.filter(pl.col('valid_at')>datetime(2013,4,1))
    gw,bw=fit_lead_weights(early.select('baseline','mlp').to_numpy(),early['actual'],early['lead'])
    grid=[0.,.25,.5,.75,1.]
    choices=[dict(strength=a,**metrics(late['actual'],predict_lead_blend(
        late.select('baseline','mlp').to_numpy(),late['lead'],gw,bw,a))) for a in grid]
    selected=min(choices,key=lambda r:(r['RMSE'],r['strength']))['strength']
    protocol=dict(baseline=str(BASE),bins=['1-6','7-12','13-18','19-24'],grid=grid,candidates=choices,
        selected_strength=selected,selection='Jan-Mar weights -> Apr-Jun shrinkage selection; then refit bins Jan-Jun',
        caveat='Base models and original blend reused full validation; historical test is exploratory')
    (OUT/'protocol.json').write_text(json.dumps(protocol,indent=2))
    if selected==0:
        (OUT/'decision.json').write_text(json.dumps(dict(accepted=False,reason='Temporal validation selected global weights'),indent=2))
        print('Discarded at temporal validation gate; test unopened.')
        return
    _,bw=fit_lead_weights(va.select('baseline','mlp').to_numpy(),va['actual'],va['lead'])
    old=json.loads((BASE/'weights.json').read_text())
    gw=np.array([old['baseline'],old['mlp']])
    (OUT/'weights.json').write_text(json.dumps(dict(global_weights=gw.tolist(),bin_weights={str(k):v.tolist() for k,v in bw.items()},strength=selected,validation_samples=va.height),indent=2))
    test=pl.read_csv(BASE/'predictions.csv',try_parse_dates=True)
    prediction=predict_lead_blend(test.select('baseline','mlp').to_numpy(),test['lead'],gw,bw,selected)
    result=test.select(KEYS).with_columns(pl.Series('baseline',test['mlp_blend']),pl.Series('lead_blend',prediction))
    scores=[dict(model=n,**metrics(result['actual'],result[n])) for n in ['baseline','lead_blend']]
    paired=paired_comparison(result,'lead_blend')
    accepted=scores[1]['RMSE']<scores[0]['RMSE'] and paired['delta_RMSE_high']<0
    pl.DataFrame(scores).write_csv(OUT/'metrics.csv')
    (OUT/'paired.json').write_text(json.dumps(paired,indent=2))
    (OUT/'decision.json').write_text(json.dumps(dict(accepted=accepted,scores=scores),indent=2))
    if accepted:
        result.write_csv(OUT/'predictions.csv')
        va.select(KEYS).with_columns(pl.Series('baseline',va['mlp_blend']),pl.Series('lead_blend',predict_lead_blend(
            va.select('baseline','mlp').to_numpy(),va['lead'],gw,bw,selected))).write_csv(OUT/'validation_predictions.csv')
    print(json.dumps(dict(accepted=accepted,strength=selected,scores=scores,paired=paired),indent=2))


if __name__=='__main__':main()

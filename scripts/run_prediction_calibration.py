"""Temporally gated isotonic calibration of the current accepted blend."""
from datetime import datetime
import json
from pathlib import Path
import joblib
import numpy as np
import polars as pl
from sklearn.isotonic import IsotonicRegression
from windpower.evaluation.metrics import metrics
from windpower.evaluation.paired import paired_comparison

PARENT=Path('artifacts/kpower_ablation/ablation_20261114_v1')
BASE=PARENT/'curve_residual_v1'
OUT=PARENT/'prediction_calibration_v1'
COLUMN='curve_blend'


def fit(frame):
    return IsotonicRegression(y_min=0,y_max=1,out_of_bounds='clip').fit(frame[COLUMN].to_numpy(),frame['actual'].to_numpy())


def main():
    OUT.mkdir(exist_ok=False)
    va=pl.read_csv(BASE/'validation_predictions.csv',try_parse_dates=True)
    early=va.filter(pl.col('valid_at')<=datetime(2013,4,1))
    late=va.filter(pl.col('valid_at')>datetime(2013,4,1))
    grid=[0.,.25,.5,.75,1.]
    calibrator=fit(early)
    raw=late[COLUMN].to_numpy(); calibrated=calibrator.predict(raw)
    candidates=[dict(strength=a,**metrics(late['actual'],(1-a)*raw+a*calibrated)) for a in grid]
    selected=min(candidates,key=lambda r:(r['RMSE'],r['strength']))['strength']
    protocol=dict(method='Global monotone prediction calibration',baseline=str(BASE),grid=grid,
        split='Jan-Mar curve fitting, Apr-Jun strength selection, then Jan-Jun curve refit',
        candidates=candidates,selected_strength=selected,
        caveat='Base models and blend used full validation; fixed historical test is exploratory')
    (OUT/'protocol.json').write_text(json.dumps(protocol,indent=2))
    if selected==0:
        (OUT/'decision.json').write_text(json.dumps(dict(accepted=False,reason='Temporal validation rejects calibration'),indent=2))
        print('Discarded at temporal validation gate; test unopened.')
        return
    calibrator=fit(va)
    joblib.dump(dict(calibrator=calibrator,strength=selected,protocol=protocol),OUT/'calibrator.joblib')
    (OUT/'weights.json').write_text(json.dumps(dict(raw=1-selected,calibrated=selected,validation_samples=va.height),indent=2))
    # No test source is opened before calibration and strength are fixed.
    test=pl.read_csv(BASE/'predictions.csv',try_parse_dates=True)
    raw=test[COLUMN].to_numpy()
    test=test.select('origin','valid_at','lead','actual','site').with_columns(
        pl.Series('baseline',raw),pl.Series('calibrated',(1-selected)*raw+selected*calibrator.predict(raw)))
    scores=[dict(model=n,**metrics(test['actual'],test[n])) for n in ['baseline','calibrated']]
    paired=paired_comparison(test,'calibrated')
    accepted=scores[1]['RMSE']<scores[0]['RMSE'] and paired['delta_RMSE_high']<0
    pl.DataFrame(scores).write_csv(OUT/'metrics.csv')
    (OUT/'paired.json').write_text(json.dumps(paired,indent=2))
    (OUT/'decision.json').write_text(json.dumps(dict(accepted=accepted,scores=scores),indent=2))
    if accepted:
        test.write_csv(OUT/'predictions.csv')
        raw=va[COLUMN].to_numpy()
        va.select('origin','valid_at','lead','actual','site').with_columns(pl.Series('baseline',raw),
            pl.Series('calibrated',(1-selected)*raw+selected*calibrator.predict(raw))).write_csv(OUT/'validation_predictions.csv')
    print(json.dumps(dict(accepted=accepted,selected_strength=selected,scores=scores,paired=paired),indent=2))


if __name__=='__main__':main()

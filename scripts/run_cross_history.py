"""Fixed cross-site past-power features with validation-only convex blending."""
import json
from pathlib import Path

import joblib
import numpy as np
import polars as pl
from lightgbm import LGBMRegressor, early_stopping
from sqlalchemy import text

from windpower.db.core import engine
from windpower.features.build import build
from windpower.features.cross_nwp import temporal_spatial_panel,attach_temporal,COLUMNS,TEMPORAL_COLUMNS
from windpower.features.cross_history import history_panel,attach_history,COLUMNS as HISTORY
from windpower.models.train import config,partitions
from windpower.models import kpower_reproduction as kp
from windpower.models.validation_blend import fit_weights
from windpower.evaluation.metrics import metrics
from windpower.evaluation.paired import paired_comparison

ID='ablation_20261114_v1'
PARENT=Path('artifacts/kpower_ablation')/ID
BASE=PARENT/'temporal_nwp_v1'
OUT=PARENT/'cross_history_v1'
FEATURES=kp.STAGE2+COLUMNS+TEMPORAL_COLUMNS+HISTORY
KEYS=['origin','valid_at','lead','actual','site']


def main():
    OUT.mkdir(exist_ok=False)
    cfg=config()
    protocol=dict(config=cfg,features=FEATURES,baseline=str(BASE),
        history='Exact origin-1..6 hours, strictly before origin; missing history stays null with count',
        availability='All-site past observations assumed available; actual arrival timestamps unknown',
        validation='Early stopping and pooled convex blend MSE; weights frozen before test scoring',
        caveat='Exploratory fixed historical test; validation reused for early stopping')
    (OUT/'protocol.json').write_text(json.dumps(protocol,indent=2))
    stage1=joblib.load(next(Path(f'artifacts/kpower_reproduction/{ID}_full/site_1').glob('*.joblib')))['stage1']
    panels,parts,oofs,crosses={},{},[],[]
    for site in range(1,11):
        panel=kp.nwp_panel(site)
        panels[site]=panel
        parts[site]={k:kp.attach(v,panel) for k,v in partitions(build(site)).items()}
        oofs.append(kp.stage1_oof(parts[site]['train'],panel,cfg['lightgbm'],site))
        crosses.append(panel.select('origin','valid_at').with_columns(pl.Series(f'stage1_{site}',
            np.clip(stage1[site].predict(panel.select(kp.STAGE1).to_numpy()),0,1))))
        print(f'Prepared zone {site}',flush=True)
    spatial=temporal_spatial_panel(panels)
    observations={site:{} for site in range(1,11)}
    with engine().connect() as connection:
        for row in connection.execute(text('SELECT farm_id,observed_at,power FROM power_observations')):
            observations[row.farm_id][row.observed_at]=row.power
    history=history_panel(panels[1]['origin'].unique().to_list(),observations)
    def attach(frame):
        return attach_history(attach_temporal(frame,spatial),history)
    train_cross,future_cross=kp.combine(oofs),kp.combine(crosses)
    models,validations={},[]
    for site in range(1,11):
        tr=parts[site]['train'].join(train_cross,on=['origin','valid_at'],how='inner',validate='1:1').sort('origin','valid_at')
        expected=pl.read_csv(f'artifacts/kpower_reproduction/{ID}_full/site_{site}/train_keys.csv',try_parse_dates=True)
        if not tr.select('origin','valid_at').equals(expected):
            raise ValueError('Training keys differ')
        tr=attach(tr)
        va=attach(kp.add_cross_site(parts[site]['validation'],future_cross))
        model=LGBMRegressor(**cfg['lightgbm'])
        model.fit(tr.select(FEATURES).to_numpy(),tr['actual'].to_numpy(),
            eval_set=[(va.select(FEATURES).to_numpy(),va['actual'].to_numpy())],callbacks=[early_stopping(30,verbose=False)])
        models[site]=model
        joblib.dump(dict(model=model,columns=FEATURES,protocol=protocol),OUT/f'site_{site}.joblib')
        validations.append(va.select('origin','valid_at','lead','actual').with_columns(pl.lit(site).alias('site'),
            pl.Series('history',np.clip(model.predict(va.select(FEATURES).to_numpy()),0,1))))
        print(f'Fitted zone {site}',flush=True)
    validation=pl.concat(validations)
    old_validation=pl.read_csv(BASE/'validation_predictions.csv',try_parse_dates=True)
    if not validation.select(KEYS).equals(old_validation.select(KEYS)):
        raise ValueError('Validation keys/labels differ')
    baseline_v=old_validation['temporal_blend'].to_numpy()
    weights=fit_weights(np.column_stack([baseline_v,validation['history'].to_numpy()]),validation['actual'].to_numpy())
    (OUT/'weights.json').write_text(json.dumps(dict(baseline=float(weights[0]),history=float(weights[1]),validation_samples=validation.height),indent=2))
    # Test inference only after all models and blend weights are fixed.
    tests=[]
    for site in range(1,11):
        te=attach(kp.add_cross_site(parts[site]['test'],future_cross))
        tests.append(te.select('origin','valid_at','lead','actual').with_columns(pl.lit(site).alias('site'),
            pl.Series('history',np.clip(models[site].predict(te.select(FEATURES).to_numpy()),0,1))))
    test=pl.concat(tests)
    previous=pl.read_csv(BASE/'predictions.csv',try_parse_dates=True)
    if not test.select(KEYS).equals(previous.select(KEYS)):
        raise ValueError('Test keys/labels differ')
    test=test.with_columns(pl.Series('baseline',previous['temporal_blend']))
    test=test.with_columns((pl.col('baseline')*weights[0]+pl.col('history')*weights[1]).alias('history_blend'))
    scores=[dict(model=n,**metrics(test['actual'],test[n])) for n in ['baseline','history','history_blend']]
    paired=paired_comparison(test,'history_blend')
    accepted=scores[-1]['RMSE']<scores[0]['RMSE'] and paired['delta_RMSE_high']<0
    pl.DataFrame(scores).write_csv(OUT/'metrics.csv')
    (OUT/'paired.json').write_text(json.dumps(paired,indent=2))
    (OUT/'decision.json').write_text(json.dumps(dict(accepted=accepted,scores=scores),indent=2))
    if accepted:
        test.write_csv(OUT/'predictions.csv')
        validation.with_columns(pl.Series('baseline',baseline_v),pl.Series('history_blend',baseline_v*weights[0]+validation['history'].to_numpy()*weights[1])).write_csv(OUT/'validation_predictions.csv')
    print(json.dumps(dict(accepted=accepted,weights=weights.tolist(),scores=scores,paired=paired),indent=2),flush=True)


if __name__=='__main__':
    main()

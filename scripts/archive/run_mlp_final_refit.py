"""Frozen-epoch final refit replacing only the accepted MLP branch."""
from datetime import datetime
import json
from pathlib import Path
import joblib
import numpy as np
import polars as pl
from sklearn.neural_network import MLPRegressor
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

from run_shared_final_refit import past_oof
from windpower.features.build import build
from windpower.features.cross_nwp import temporal_spatial_panel,attach_temporal
from windpower.models.train import config,partitions
from windpower.models import kpower_reproduction as kp
from windpower.evaluation.metrics import metrics
from windpower.evaluation.paired import paired_comparison

ID='ablation_20261114_v1'
PARENT=Path('artifacts/kpower_ablation')/ID
BASE=PARENT/'shared_final_refit_v1'
OUT=PARENT/'mlp_final_refit_v1'
KEYS=['origin','valid_at','lead','actual','site']


def main():
    registry=json.loads(Path('docs/研究记录/optimization-progress.json').read_text())
    if registry['milestones'][-1]['id']!='shared_final_refit':raise ValueError('Baseline changed')
    OUT.mkdir(exist_ok=False)
    cfg=config();cutoff=datetime.fromisoformat(cfg['splits']['test'][0])
    original=joblib.load(PARENT/'mlp_shared_v1/model.joblib')
    features=original['columns']
    epochs=json.loads((PARENT/'mlp_shared_v1/verification.json').read_text())['best_internal_validation_epoch']
    params=dict(original['model'][-1].get_params(),early_stopping=False,max_iter=epochs,n_iter_no_change=epochs+1,tol=0.)
    coefficient=json.loads((PARENT/'mlp_shared_v1/weights.json').read_text())['mlp']
    stage1=joblib.load(BASE/'model.joblib')['stage1']
    protocol=dict(method='Replace only original MLP after final train+validation refit; preserve accepted shared refit',
        params=params,features=features,frozen_epochs=epochs,fixed_branch_coefficient=coefficient,
        cutoff=str(cutoff),label_rule='All final labels strictly before first test origin',
        selection='No new epoch, architecture, or weight selection; original best internal epoch retained',
        protocol_change='Former validation labels become fitting data; no independent validation after this refit',
        caveat='Exploratory historical test; actual NWP/observation arrival timestamps unknown')
    (OUT/'protocol.json').write_text(json.dumps(protocol,indent=2))
    panels,parts,extended,oofs,crosses,audit={},{},{},[],[],[]
    for site in range(1,11):
        panel=kp.nwp_panel(site);panels[site]=panel
        parts[site]={k:kp.attach(v,panel) for k,v in partitions(build(site)).items()}
        extended[site]=pl.concat([parts[site]['train'],parts[site]['validation']]).filter(pl.col('valid_at')<cutoff).sort('origin','valid_at')
        oof,proof=past_oof(extended[site],panel,cfg['lightgbm'],site)
        oofs.append(oof);audit.extend(proof)
        crosses.append(panel.select('origin','valid_at').with_columns(pl.Series(f'stage1_{site}',
            np.clip(stage1[site].predict(panel.select(kp.STAGE1).to_numpy()),0,1))))
        print(f'Prepared MLP final-refit zone {site}',flush=True)
    (OUT/'oof_audit.json').write_text(json.dumps(audit,indent=2))
    spatial=temporal_spatial_panel(panels)
    train_cross,test_cross=kp.combine(oofs),kp.combine(crosses)
    trains=[]
    for site in range(1,11):
        tr=extended[site].join(train_cross,on=['origin','valid_at'],how='inner',validate='1:1').sort('origin','valid_at')
        trains.append(attach_temporal(tr,spatial).with_columns(pl.lit(site).alias('farm')))
    tr=pl.concat(trains)
    if tr['valid_at'].max()>=cutoff:raise ValueError('Final labels cross test origin')
    def design(frame):
        farms=frame['farm'].to_numpy().astype(int)
        if ((farms<1)|(farms>10)).any():raise ValueError('Unknown farm')
        return np.column_stack([frame.select(features).to_numpy(),np.eye(10)[farms-1]])
    model=make_pipeline(StandardScaler(),MLPRegressor(**params))
    print(f'Final MLP fitting: {tr.height} rows, {epochs} frozen epochs',flush=True)
    with threadpool_limits(limits=2):
        model.fit(design(tr),tr['actual'].to_numpy())
    if model[-1].n_iter_!=epochs:raise ValueError('Frozen epoch count changed')
    joblib.dump(dict(model=model,stage1=stage1,columns=features,protocol=protocol),OUT/'model.joblib')
    (OUT/'weights.json').write_text(json.dumps(dict(mlp_branch=coefficient,frozen_epochs=epochs,
        train_samples=tr.height,train_first=str(tr['valid_at'].min()),train_last=str(tr['valid_at'].max())),indent=2))
    tests=[]
    for site in range(1,11):
        tests.append(attach_temporal(kp.add_cross_site(parts[site]['test'],test_cross),spatial)
            .with_columns(pl.lit(site).alias('farm'),pl.lit(site).alias('site')))
    te=pl.concat(tests)
    previous=pl.read_csv(BASE/'predictions.csv',try_parse_dates=True)
    old_mlp=pl.read_csv(PARENT/'mlp_shared_v1/predictions.csv',try_parse_dates=True)
    if not te.select(KEYS).equals(previous.select(KEYS)) or not te.select(KEYS).equals(old_mlp.select(KEYS)):
        raise ValueError('Test keys/labels differ')
    with threadpool_limits(limits=2):
        replacement=np.clip(model.predict(design(te)),0,1)
    retained=previous['final_refit'].to_numpy()-coefficient*old_mlp['mlp'].to_numpy()
    if (retained < -1e-12).any() or (retained>1-coefficient+1e-12).any():raise ValueError('Invalid MLP branch decomposition')
    result=te.select(KEYS).with_columns(pl.Series('baseline',previous['final_refit']),pl.Series('refit_mlp',replacement),
        pl.Series('mlp_final_refit',np.clip(retained+coefficient*replacement,0,1)))
    scores=[dict(model=n,**metrics(result['actual'],result[n])) for n in ['baseline','refit_mlp','mlp_final_refit']]
    paired=paired_comparison(result,'mlp_final_refit')
    accepted=scores[-1]['RMSE']<scores[0]['RMSE'] and paired['delta_RMSE_high']<0
    pl.DataFrame(scores).write_csv(OUT/'metrics.csv')
    (OUT/'paired.json').write_text(json.dumps(paired,indent=2))
    (OUT/'decision.json').write_text(json.dumps(dict(accepted=accepted,scores=scores),indent=2))
    if accepted:result.write_csv(OUT/'predictions.csv')
    print(json.dumps(dict(accepted=accepted,scores=scores,paired=paired),indent=2),flush=True)


if __name__=='__main__':main()

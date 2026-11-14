"""Fixed three-seed standardized MLP average with validation-only blend."""
import json
from pathlib import Path

import joblib
import numpy as np
import polars as pl
from sklearn.neural_network import MLPRegressor
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

from windpower.features.build import build
from windpower.features.cross_nwp import temporal_spatial_panel,attach_temporal,COLUMNS,TEMPORAL_COLUMNS
from windpower.models.train import config,partitions
from windpower.models import kpower_reproduction as kp
from windpower.models.validation_blend import fit_weights
from windpower.evaluation.metrics import metrics
from windpower.evaluation.paired import paired_comparison

ID='ablation_20261114_v1'
PARENT=Path('artifacts/kpower_ablation')/ID
BASE=PARENT/'mlp_shared_v1'
OUT=PARENT/'mlp_seed_ensemble_v1'
FEATURES=kp.STAGE2+COLUMNS+TEMPORAL_COLUMNS
KEYS=['origin','valid_at','lead','actual','site']


def main():
    OUT.mkdir(exist_ok=False)
    cfg=config()
    params=dict(hidden_layer_sizes=(128,64),activation='relu',alpha=.1,batch_size=512,learning_rate_init=.001,max_iter=200,early_stopping=True,validation_fraction=.15,n_iter_no_change=15)
    seeds=[17,73,121]
    protocol=dict(params=params,seeds=seeds,features=FEATURES,farm_encoding='fixed one-hot identities 1..10',baseline=str(BASE),
        method='Equal mean of three fixed-seed train-only StandardScaler/MLP models; same architecture, no seed selection',
        selection='MLP internal stopping uses only original training labels; external validation fits convex blend only',
        caveat='Exploratory historical test, validation reused for early stopping')
    (OUT/'protocol.json').write_text(json.dumps(protocol,indent=2))
    stage1=joblib.load(next(Path(f'artifacts/kpower_reproduction/{ID}_full/site_1').glob('*.joblib')))['stage1']
    panels,parts,oofs,crosses={},{},[],[]
    for site in range(1,11):
        panel=kp.nwp_panel(site); panels[site]=panel
        parts[site]={k:kp.attach(v,panel) for k,v in partitions(build(site)).items()}
        oofs.append(kp.stage1_oof(parts[site]['train'],panel,cfg['lightgbm'],site))
        crosses.append(panel.select('origin','valid_at').with_columns(pl.Series(f'stage1_{site}',
            np.clip(stage1[site].predict(panel.select(kp.STAGE1).to_numpy()),0,1))))
        print(f'Prepared zone {site}',flush=True)
    spatial=temporal_spatial_panel(panels)
    cross_train,cross_future=kp.combine(oofs),kp.combine(crosses)
    trains,validations=[],[]
    for site in range(1,11):
        tr=parts[site]['train'].join(cross_train,on=['origin','valid_at'],how='inner',validate='1:1').sort('origin','valid_at')
        expected=pl.read_csv(f'artifacts/kpower_reproduction/{ID}_full/site_{site}/train_keys.csv',try_parse_dates=True)
        if not tr.select('origin','valid_at').equals(expected):raise ValueError('Training keys differ')
        trains.append(attach_temporal(tr,spatial).with_columns(pl.lit(site).alias('farm')))
        validations.append(attach_temporal(kp.add_cross_site(parts[site]['validation'],cross_future),spatial)
            .with_columns(pl.lit(site).alias('farm'),pl.lit(site).alias('site')))
    tr,va=pl.concat(trains),pl.concat(validations)
    def design(frame):
        farms=frame['farm'].to_numpy().astype(int)
        if ((farms<1)|(farms>10)).any():
            raise ValueError('Unknown farm identity')
        return np.column_stack([frame.select(FEATURES).to_numpy(),np.eye(10)[farms-1]])
    models=[]
    x,y=design(tr),tr['actual'].to_numpy()
    for seed in seeds:
        model=make_pipeline(StandardScaler(),MLPRegressor(**dict(params,random_state=seed)))
        print(f'Training MLP seed {seed}: {tr.height} rows',flush=True)
        with threadpool_limits(limits=2):
            model.fit(x,y)
        models.append(model)
        joblib.dump(model,OUT/f'seed_{seed}.joblib')
        print(f'Finished seed {seed}: {model[-1].n_iter_} epochs',flush=True)
    joblib.dump(dict(models=models,columns=FEATURES,protocol=protocol),OUT/'model.joblib')
    def predict(frame):
        x=design(frame)
        with threadpool_limits(limits=2):
            return np.mean([np.clip(m.predict(x),0,1) for m in models],axis=0)
    candidate=predict(va)
    old=pl.read_csv(BASE/'validation_predictions.csv',try_parse_dates=True)
    if not va.select(KEYS).equals(old.select(KEYS)):raise ValueError('Validation keys/labels differ')
    baseline_v=old['mlp_blend'].to_numpy()
    weights=fit_weights(np.column_stack([baseline_v,candidate]),va['actual'].to_numpy())
    (OUT/'weights.json').write_text(json.dumps(dict(baseline=float(weights[0]),seed_average=float(weights[1]),
        train_samples=tr.height,validation_samples=va.height,iterations=[int(m[-1].n_iter_) for m in models]),indent=2))
    if weights[1] < 1e-8:
        (OUT/'decision.json').write_text(json.dumps(dict(accepted=False,reason='Validation chose zero candidate weight'),indent=2))
        print('Discarded at validation gate; test unopened.',flush=True)
        return
    # All model rounds and weights fixed before test inference.
    tests=[]
    for site in range(1,11):
        tests.append(attach_temporal(kp.add_cross_site(parts[site]['test'],cross_future),spatial)
            .with_columns(pl.lit(site).alias('farm'),pl.lit(site).alias('site')))
    te=pl.concat(tests)
    previous=pl.read_csv(BASE/'predictions.csv',try_parse_dates=True)
    if not te.select(KEYS).equals(previous.select(KEYS)):raise ValueError('Test keys/labels differ')
    test=te.select(KEYS).with_columns(pl.Series('baseline',previous['mlp_blend']),
        pl.Series('seed_average',predict(te)))
    test=test.with_columns((pl.col('baseline')*weights[0]+pl.col('seed_average')*weights[1]).alias('seed_blend'))
    scores=[dict(model=n,**metrics(test['actual'],test[n])) for n in ['baseline','seed_average','seed_blend']]
    paired=paired_comparison(test,'seed_blend')
    accepted=scores[-1]['RMSE']<scores[0]['RMSE'] and paired['delta_RMSE_high']<0
    pl.DataFrame(scores).write_csv(OUT/'metrics.csv')
    (OUT/'paired.json').write_text(json.dumps(paired,indent=2))
    (OUT/'decision.json').write_text(json.dumps(dict(accepted=accepted,scores=scores),indent=2))
    if accepted:
        test.write_csv(OUT/'predictions.csv')
        va.select(KEYS).with_columns(pl.Series('baseline',baseline_v),pl.Series('seed_average',candidate),
            pl.Series('seed_blend',baseline_v*weights[0]+candidate*weights[1])).write_csv(OUT/'validation_predictions.csv')
    print(json.dumps(dict(accepted=accepted,weights=weights.tolist(),scores=scores,paired=paired),indent=2),flush=True)


if __name__=='__main__':main()

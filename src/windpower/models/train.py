from pathlib import Path
from datetime import datetime
import json
import uuid
import joblib
import numpy as np
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import Ridge
from lightgbm import LGBMRegressor, early_stopping
from windpower.features.build import GROUPS, build, split
from windpower.db.core import engine, upsert
from windpower.db.schema import model_runs, dataset_splits

class Persistence:
    def predict(self, x): return x[:,0]

def config(): return json.loads(Path('configs/experiment.json').read_text())
def partitions(frame):
    cfg=config();out={}
    for name,(a,b) in cfg['splits'].items():
        out[name]=split(frame,datetime.fromisoformat(a),datetime.fromisoformat(b))
        if out[name].is_empty(): raise ValueError(f'Empty {name} split')
    return out

def train(site):
    cfg=config();parts=partitions(build(site)); tr,va=parts['train'],parts['validation']
    folder=Path(f'artifacts/site_{site}');folder.mkdir(parents=True,exist_ok=True)
    runs=[]
    with engine().begin() as c:
        upsert(c,dataset_splits,[dict(name=k,start_at=datetime.fromisoformat(v[0]),end_at=datetime.fromisoformat(v[1]),semantics='target (start,end], complete daily origins; no cross-boundary labels') for k,v in cfg['splits'].items()])
    for name,group in [('persistence','C'),('ridge','C'),('lightgbm','A'),('lightgbm','B'),('lightgbm','C')]:
        cols=['lag1'] if name=='persistence' else GROUPS[group]
        x,y=tr.select(cols).to_numpy(),tr['actual'].to_numpy()
        vx,vy=va.select(cols).to_numpy(),va['actual'].to_numpy()
        selected={}
        if name=='persistence': model=Persistence()
        elif name=='ridge':
            candidates=[]
            for alpha in cfg['ridge_alphas']:
                m=make_pipeline(StandardScaler(),Ridge(alpha=alpha));m.fit(x,y)
                candidates.append((float(np.mean((np.clip(m.predict(vx),0,1)-vy)**2)),alpha,m))
            _,alpha,model=min(candidates,key=lambda t:t[0]);selected={'alpha':alpha}
        else:
            model=LGBMRegressor(**cfg['lightgbm'])
            model.fit(x,y,eval_set=[(vx,vy)],callbacks=[early_stopping(30,verbose=False)])
            selected={'best_iteration':model.best_iteration_}
        run_id=str(uuid.uuid4());path=folder/f'{run_id}.joblib'
        info=dict(config=cfg,selected=selected,columns=cols,train_samples=tr.height,validation_samples=va.height,
                  train_target_range=[str(tr['valid_at'].min()),str(tr['valid_at'].max())],
                  validation_target_range=[str(va['valid_at'].min()),str(va['valid_at'].max())],
                  history_policy='strictly before origin',availability_assumption='daily source-clock midnight, NWP available for next 24h; actual issuance unknown',
                  validation_mae=float(np.mean(np.abs(np.clip(model.predict(vx),0,1)-vy))))
        joblib.dump(dict(model=model,columns=cols,metadata=info),path)
        row=dict(id=run_id,farm_id=site,model=name,feature_group=group,created_at=datetime.now(),config=info,artifact=str(path),metrics=None)
        with engine().begin() as c: upsert(c,model_runs,[row])
        runs.append(run_id)
    (folder/'latest_runs.json').write_text(json.dumps(runs,indent=2))
    return runs

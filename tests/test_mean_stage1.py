from datetime import datetime,timedelta
import numpy as np
import polars as pl
import pytest
from windpower.models import mean_stage1 as mean


def test_mean_oof_excludes_block_and_origin_labels(monkeypatch):
    monkeypatch.setattr(mean,'MIN_TRAIN_DAYS',2)
    monkeypatch.setattr(mean,'OOF_BLOCK_DAYS',1)
    class Model:
        def fit(self,x,y):self.value=np.mean(y)
        def predict(self,x):return np.repeat(self.value,len(x))
    monkeypatch.setattr(mean,'estimator',lambda *_args:Model())
    start=datetime(2012,1,1)
    rows=[]
    for i,y in enumerate([0.,1.,.9,.2,.1]):
        origin=start+timedelta(days=i)
        rows.append(dict(origin=origin,valid_at=origin+timedelta(hours=1),actual=y,**{c:1. for c in mean.STAGE1}))
    cutoff=start+timedelta(days=2)
    rows.append(dict(origin=start+timedelta(days=1),valid_at=cutoff,actual=.9,**{c:1. for c in mean.STAGE1}))
    tr=pl.DataFrame(rows)
    first=mean.mean_oof(tr,tr.drop('actual'),{},1).filter(pl.col('origin')==cutoff)['stage1_mean_1'][0]
    assert first==pytest.approx(.5)
    changed=tr.with_columns(pl.when(pl.col('valid_at')>=cutoff).then(0.).otherwise(pl.col('actual')).alias('actual'))
    again=mean.mean_oof(changed,changed.drop('actual'),{},1).filter(pl.col('origin')==cutoff)['stage1_mean_1'][0]
    assert again==pytest.approx(first)

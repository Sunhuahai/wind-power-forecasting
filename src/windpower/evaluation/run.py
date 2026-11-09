from pathlib import Path
import json
from datetime import datetime
import joblib
import numpy as np
import polars as pl
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from sqlalchemy import select, update
from windpower.db.core import engine, upsert
from windpower.db.schema import model_runs,predictions
from windpower.features.build import build
from windpower.models.train import partitions
from windpower.evaluation.metrics import metrics

def latest(site): return json.loads(Path(f'artifacts/site_{site}/latest_runs.json').read_text())
def predict(site,run_ids=None):
    test=partitions(build(site))['test'];out=Path(f'artifacts/site_{site}')
    for rid in run_ids or latest(site):
        with engine().connect() as c:
            run=c.execute(select(model_runs).where(model_runs.c.id==rid,model_runs.c.farm_id==site)).mappings().one()
        bundle=joblib.load(run['artifact'])
        pred=np.clip(bundle['model'].predict(test.select(bundle['columns']).to_numpy()),0,1)
        f=test.select(['origin','valid_at','lead','actual']).with_columns(pl.Series('prediction',pred))
        rows=[dict(run_id=rid,**r) for r in f.iter_rows(named=True)]
        with engine().begin() as c: upsert(c,predictions,rows)
        f.write_csv(out/f'{rid}_predictions.csv')
    return test.height

def evaluate(site,run_ids=None):
    out=Path(f'artifacts/site_{site}'); summaries=[];leads=[];series=[]
    for rid in run_ids or latest(site):
        with engine().connect() as c:
            run=c.execute(select(model_runs).where(model_runs.c.id==rid,model_runs.c.farm_id==site)).mappings().one()
            start,end=[datetime.fromisoformat(t) for t in run['config']['config']['splits']['test']]
            rows=c.execute(select(predictions).where(predictions.c.run_id==rid, predictions.c.origin>=start, predictions.c.valid_at>start, predictions.c.valid_at<=end).order_by(predictions.c.valid_at)).mappings().all()
        if not rows: raise ValueError('Run predict first')
        f=pl.DataFrame([dict(r) for r in rows]);score=metrics(f['actual'],f['prediction'])
        with engine().begin() as c: c.execute(update(model_runs).where(model_runs.c.id==rid).values(metrics=score))
        label=f"{run['model']}_{run['feature_group']}"
        summaries.append(dict(site=site,run_id=rid,model=label,**score))
        for lead in range(1,25):
            part=f.filter(pl.col('lead')==lead)
            leads.append(dict(site=site,model=label,lead=lead,**metrics(part['actual'],part['prediction'])))
        series.append((label,f))
        if run['model']=='lightgbm':
            b=joblib.load(run['artifact']); importance=b['model'].booster_.feature_importance(importance_type='gain')
            order=np.argsort(importance)[-15:]
            fig,ax=plt.subplots(figsize=(9,6));ax.barh(np.array(b['columns'])[order],importance[order]);ax.set_xlabel('Split gain');ax.set_title(label)
            fig.tight_layout();fig.savefig(out/f'{label}_importance.png',dpi=150);plt.close(fig)
    # Refuse comparisons across different target sets.
    keys=[f.select('origin','valid_at').rows() for _,f in series]
    if any(k!=keys[0] for k in keys): raise ValueError('Inconsistent comparison samples')
    pl.DataFrame(summaries).write_csv(out/'metrics.csv');pl.DataFrame(leads).write_csv(out/'lead_metrics.csv')
    fig,ax=plt.subplots(figsize=(10,5));ax.bar([s['model'] for s in summaries],[s['RMSE'] for s in summaries]);ax.set_ylabel('Normalized RMSE');ax.tick_params(axis='x',rotation=20);fig.tight_layout();fig.savefig(out/'model_comparison.png',dpi=150);plt.close(fig)
    fig,ax=plt.subplots(figsize=(10,5))
    for name,_ in series:
        q=[r for r in leads if r['model']==name];ax.plot([r['lead'] for r in q],[r['RMSE'] for r in q],label=name)
    ax.set(xlabel='Lead (hours)',ylabel='Normalized RMSE');ax.legend();fig.tight_layout();fig.savefig(out/'lead_errors.png',dpi=150);plt.close(fig)
    fig,ax=plt.subplots(figsize=(13,5));f=series[0][1].head(168);ax.plot(f['valid_at'],f['actual'],color='black',label='actual')
    for name,f in series: ax.plot(f['valid_at'].head(168),f['prediction'].head(168),label=name,alpha=.7)
    ax.set_ylabel('Power / nominal capacity');ax.legend();fig.autofmt_xdate();fig.tight_layout();fig.savefig(out/'prediction_curve.png',dpi=150);plt.close(fig)
    report(site,summaries,series[0][1]); return summaries

def report(site,summaries,test):
    out=Path(f'artifacts/site_{site}')
    quality=json.loads((out/'quality_source.json').read_text())
    with engine().connect() as c:
        run=c.execute(select(model_runs).where(model_runs.c.id==summaries[-1]['run_id'])).mappings().one()
    cfg=run['config']
    lines=[f'# Zone {site} 实际实验报告','',f'原始数据 {quality["rows"]} 行，范围 {quality["start"]} 至 {quality["end"]}；重复 {quality["duplicates"]}，时间缺口 {quality["gap_count"]}，字段问题 {quality["field_issues"]}。',
      '',f'训练 {cfg["train_samples"]} 个小时样本；验证 {cfg["validation_samples"]}；独立测试 {test.height}（{test["origin"].n_unique()} 个完整日）。',
      '',f'训练目标时间：{cfg["train_target_range"]}；验证：{cfg["validation_target_range"]}；测试：{test["valid_at"].min()} 至 {test["valid_at"].max()}。',
      '', '功率为容量归一化 0–1，因此 nMAE=MAE、nRMSE=RMSE；乘 100 可读为额定容量百分比。全部模型输出裁剪到 [0,1]，使用完全一致的测试起点及目标。',
      '', '|模型|MAE|RMSE|n|','|---|---:|---:|---:|']
    for s in summaries:lines.append(f'|{s["model"]}|{s["MAE"]:.6f}|{s["RMSE"]:.6f}|{s["n"]}|')
    lgb={s['model'][-1]:s for s in summaries if s['model'].startswith('lightgbm')}
    lines+=['',f'物理特征 B 相对 A 的 RMSE 差：{lgb["B"]["RMSE"]-lgb["A"]["RMSE"]:+.6f}；历史功率 C 相对 B：{lgb["C"]["RMSE"]-lgb["B"]["RMSE"]:+.6f}（负值为改善）。仅报告实测差异，不保证每个场站改善。',
      '', '![模型比较](model_comparison.png)','![预测曲线](prediction_curve.png)','![提前量误差](lead_errors.png)',
      '', '固定配置与 Ridge 超参数、LightGBM 早停仅使用训练/验证；测试只在模型保存之后评分。2013 年 12 月标签来自官方 solution15_W.csv，并非填补或合成。',
      '', '时间为源文件无时区时间，不宣称 UTC。每日 00:00 起点及随后 1–24 小时 NWP 可用为竞赛回放假设；源包仅有有效时间，不能核实真正发布时间和到达时间。issued_at 保持 NULL。历史只允许 < origin，故 00:00 本身功率也不使用。',
      '', 'C 需要前 24 个完整历史小时；所有组共用相同完整日样本，最初两个起点及受缺失标签/历史影响的不完整日被排除。测试期过去已发生的真实功率可以作为后续预测起点历史，这模拟逐日滚动业务，不用于拟合模型。',
      '', '本机无 Docker：已提供 postgres:18.6 Compose，但本次执行使用 Homebrew PostgreSQL 18.6 独立实例。未验证 Docker 路径。没有装机容量、现场日志或真实业务延迟，暂不能宣称生产级效果。']
    (out/'report.md').write_text('\n'.join(lines),encoding='utf-8')

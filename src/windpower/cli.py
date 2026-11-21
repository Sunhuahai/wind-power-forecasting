import json
import subprocess
from pathlib import Path
from datetime import datetime
import typer
from sqlalchemy import text
from alembic import command
from alembic.config import Config
from windpower.db.core import engine, health
app=typer.Typer(no_args_is_help=True,help='GEFCom2014 PostgreSQL 实验 CLI；在项目根目录运行。')

def emit(value): typer.echo(json.dumps(value,ensure_ascii=False,indent=2,default=str))
@app.command()
def db_start():
    """使用 Docker Compose 启动 PostgreSQL 18.6。"""
    subprocess.run(['docker','compose','up','-d','--wait','db'],check=True)
@app.command()
def db_init():
    """检查版本并执行 Alembic 迁移。"""
    emit({'postgres':health()});command.upgrade(Config('alembic.ini'),'head')
@app.command()
def db_health():
    emit({'postgres':health()})
@app.command()
def download():
    from windpower.data.ingest import download as dl
    emit({'archive':str(dl())})
@app.command('import-data')
def import_data(site:int=1):
    from windpower.data.ingest import ingest
    emit(ingest(site))
@app.command()
def quality(site:int=1):
    """重新检查 PostgreSQL 中的清洗后数据；源文件问题见 quality_source.json。"""
    with engine().connect() as c:
        result=dict(c.execute(text('''SELECT count(*) AS rows, min(observed_at) AS start,
          max(observed_at) AS end, count(*) FILTER (WHERE power IS NULL) AS null_power,
          count(*) FILTER (WHERE power<0 OR power>1) AS invalid_power
          FROM power_observations WHERE farm_id=:s'''),{'s':site}).mappings().one())
        result['gaps']=c.scalar(text('''SELECT count(*) FROM (
          SELECT observed_at-lag(observed_at) OVER (ORDER BY observed_at) AS delta
          FROM power_observations WHERE farm_id=:s) q WHERE delta <> interval '1 hour' '''),{'s':site})
        result['null_weather']=c.scalar(text('''SELECT count(*) FROM nwp_forecasts WHERE farm_id=:s AND
          (u10 IS NULL OR v10 IS NULL OR u100 IS NULL OR v100 IS NULL)'''),{'s':site})
    if result['rows']==0: raise ValueError('No imported site')
    Path(f'artifacts/site_{site}/quality_db.json').write_text(json.dumps(result,default=str,indent=2))
    emit(result)
@app.command('build-samples')
def build_samples(site:int=1):
    from windpower.features.build import build
    from windpower.models.train import partitions
    parts=partitions(build(site)); counts={k:v.height for k,v in parts.items()}
    for k,v in parts.items(): v.write_parquet(f'artifacts/site_{site}/{k}_samples.parquet')
    emit(counts)
@app.command()
def train(site:int=1):
    from windpower.models.train import train as fit
    emit(fit(site))
@app.command()
def predict(site:int=1):
    """独立测试期逐日起点回放；24 小时输出写入数据库及 CSV。"""
    from windpower.evaluation.run import predict as pred
    emit({'test_samples_per_model':pred(site)})
@app.command()
def forecast(site:int=1,origin:str=typer.Option(...,help='源时间，例如 2013-12-31T00:00:00')):
    """对数据库已有 NWP 的单一起点预测，不要求未来标签。"""
    import joblib
    import numpy as np
    import polars as pl
    from sqlalchemy import select
    from windpower.features.build import build
    from windpower.evaluation.run import latest
    from windpower.db.core import upsert
    from windpower.db.schema import model_runs,predictions
    t=datetime.fromisoformat(origin)
    f=build(site,labeled=False).filter(pl.col('origin')==t)
    if f.height!=24: raise ValueError('Need complete 24h NWP and past power at requested origin')
    for rid in latest(site):
        with engine().connect() as c:run=c.execute(select(model_runs).where(model_runs.c.id==rid)).mappings().one()
        b=joblib.load(run['artifact']);p=np.clip(b['model'].predict(f.select(b['columns']).to_numpy()),0,1)
        result=f.select('origin','valid_at','lead','actual').with_columns(pl.Series('prediction',p))
        rows=[]
        for r in result.iter_rows(named=True):
            if not np.isfinite(r['actual']):r['actual']=None
            rows.append(dict(run_id=rid,**r))
        with engine().begin() as c: upsert(c,predictions,rows)
        result.write_csv(f'artifacts/site_{site}/{rid}_forecast_{t:%Y%m%d}.csv')
    emit({'origin':t,'hours':24,'models':5})
@app.command()
def evaluate(site:int=1):
    from windpower.evaluation.run import evaluate as score
    emit(score(site))
@app.command('run-all')
def run_all(site:int=1):
    """数据库已启动时，下载、迁移、导入、清洗、训练、测试和报告。"""
    db_init();download();import_data(site);quality(site);build_samples(site);train(site);predict(site);evaluate(site)
    typer.echo(f'报告: artifacts/site_{site}/report.md')
@app.command('run-sites')
def run_sites():
    """先 Zone 1，再顺序完成全部十个场站并汇总。"""
    import polars as pl
    for site in range(1,11): run_all(site)
    summarize()
@app.command()
def summarize():
    """汇总已完成的十站指标，按有效小时样本数加权。"""
    import polars as pl
    from windpower.evaluation.metrics import pooled_metrics
    all_metrics=pl.concat([pl.read_csv(f'artifacts/site_{i}/metrics.csv') for i in range(1,11)])
    all_metrics.write_csv('artifacts/all_sites_metrics.csv')
    rows=all_metrics.to_dicts()
    summary=pl.DataFrame([dict(model=name,**pooled_metrics([r for r in rows if r['model']==name]))
                         for name in sorted(set(r['model'] for r in rows))]).sort('RMSE')
    summary.write_csv('artifacts/all_sites_summary.csv')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,ax=plt.subplots(figsize=(10,5))
    ax.bar(summary['model'].to_list(),summary['RMSE'].to_list())
    ax.set_ylabel('Pooled normalized RMSE');ax.tick_params(axis='x',rotation=20)
    fig.tight_layout();fig.savefig('artifacts/all_sites_comparison.png',dpi=150);plt.close(fig)
    emit(summary.to_dicts())
@app.command('physics-experiment')
def physics_experiment(site:int=1,all_sites:bool=False,experiment_id:str|None=None):
    """独立物理融合实验；可用相同 experiment-id 续跑十站，不覆盖基线。"""
    from windpower.models.physics_experiment import run_experiment
    folder=run_experiment(range(1,11) if all_sites else [site],experiment_id)
    emit({'experiment':str(folder),'report':str(folder/'report.md')})

@app.command('evaluate-guangxi')
def evaluate_guangxi(
    farm_id: int = typer.Option(..., help='实际场站 ID'),
    target_day: str = typer.Option(..., help='北京时间业务日 YYYY-MM-DD'),
    product: str = typer.Option(..., help='day_ahead 或 ultra_short'),
    issuance_id: str | None = typer.Option(None, help='日前指定的冻结发布批次 ID'),
    model_version: str | None = typer.Option(None, help='超短期指定模型版本'),
    series_id: str | None = typer.Option(None, help='超短期指定连续预测序列'),
    power_source: str = typer.Option('scada', help='参考功率的指定数据源（须经场站确认）'),
    rule_version: str = typer.Option('GX_WIND_PUBLIC_2025',
                                     help='正式公开版或单列草案版'),
    as_of: str | None = typer.Option(None, help='评价可见截止时间（带时区 ISO 8601）'),
    save: bool = typer.Option(False, help='显式追加保存研究评价记录'),
):
    """真实 15 分钟 MW 数据的广西政策回放；缺报或豁免口径不明时不给正式分。"""
    from datetime import date
    from windpower.evaluation.guangxi_db import score_from_database
    result = score_from_database(
        farm_id=farm_id, target_date=date.fromisoformat(target_day),
        product=product, issuance_id=issuance_id,
        model_version=model_version, series_id=series_id,
        rule_version=rule_version, power_source=power_source,
        as_of=datetime.fromisoformat(as_of) if as_of else None,
        save=save,
    )
    emit(result)

@app.command('audit-f01')
def audit_f01():
    """只读审计 F01 十分钟 CSV：编码、时间网格、54 列画像与规则命中。"""
    from windpower.data.scada_f01 import audit, ARTIFACT_DIR
    report = audit()
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    (ARTIFACT_DIR / 'quality_report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str))
    emit({k: report[k] for k in ('file', 'identity', 'time_axis', 'label_candidate',
                                 'row_flag_hits', 'policy_eligibility')})

@app.command('import-f01')
def import_f01():
    """审计并版本化入库 F01 SCADA；重复导入幂等，不产生重复记录。"""
    from windpower.data.scada_f01 import ingest
    report = ingest()
    emit({k: report[k] for k in ('file', 'time_axis', 'row_flag_hits', 'database')})

@app.command('baseline-f01')
def baseline_f01(turbine_id: int = typer.Option(1, help='风机 ID')):
    """F01 原生 10 分钟基线：样本 -> 训练 -> 验证评价 -> 统一报告（测试集保持关闭）。"""
    from windpower.models.scada_baseline import run_baseline
    emit(run_baseline(turbine_id=turbine_id))

if __name__=='__main__':app()

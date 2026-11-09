"""Figures and Chinese report produced only from the independent experiment."""
from pathlib import Path
import json
import numpy as np
import polars as pl
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from windpower.evaluation.metrics import pooled_metrics
from windpower.evaluation.paired import paired_comparison
NAMES=['baseline','physics','residual','injected']
LABELS={'baseline':'Frozen LGBM C','physics':'Equivalent physics','residual':'Physics + residual','injected':'C + physics feature'}
COLORS={'baseline':'#477caa','physics':'#b29858','residual':'#b95270','injected':'#51997e'}

def save(fig,path):
    fig.tight_layout();fig.savefig(path,dpi=170,bbox_inches='tight');plt.close(fig)

def site_figures(site,out,wide,curve,train):
    speed=train['speed100'].to_numpy();actual=train['actual'].to_numpy()
    fig,ax=plt.subplots(figsize=(9,5));h=ax.hexbin(speed,actual,gridsize=50,mincnt=1,cmap='Blues',alpha=.7)
    grid=np.linspace(0,max(25,float(speed.max())),300)
    ax.plot(grid,curve.predict(grid),color='#b95270',linewidth=2,label='Train-fitted equivalent curve')
    ax.axvline(curve.cut_in,ls='--',color='#777777',label=f'cut-in={curve.cut_in:.2f}')
    ax.axvline(curve.rated,ls=':',color='#333333',label=f'rated={curve.rated:.2f}')
    ax.set(xlabel='NWP 100m wind speed (source scale)',ylabel='Normalized power',title=f'Zone {site}: training data only')
    ax.legend(fontsize=9);fig.colorbar(h,ax=ax,label='Training samples');save(fig,out/'physics_curve.png')
    fig,ax=plt.subplots(figsize=(13,5));part=wide.head(168)
    ax.plot(part['valid_at'],part['actual'],color='black',label='Actual',linewidth=1.5)
    for name in NAMES:
        if name in wide.columns:ax.plot(part['valid_at'],part[name],label=LABELS[name],color=COLORS[name],alpha=.85)
    ax.set(ylabel='Power / nominal capacity',title=f'Zone {site}: first 168 available test hours')
    ax.legend(fontsize=9,ncol=3);fig.autofmt_xdate();save(fig,out/'prediction_comparison.png')
    scores=pl.read_csv(out/'lead_metrics.csv');fig,ax=plt.subplots(figsize=(9,5))
    for name in NAMES:
        q=scores.filter(pl.col('model')==name)
        if q.height:ax.plot(q['lead'],q['RMSE'],label=LABELS[name],color=COLORS[name])
    ax.set(xlabel='Lead (hours)',ylabel='Normalized RMSE',title=f'Zone {site}');ax.legend(fontsize=9);ax.grid(alpha=.2);save(fig,out/'lead_errors.png')
    scores=pl.read_csv(out/'wind_metrics.csv');fig,ax=plt.subplots(figsize=(10,5))
    labels=scores['wind_bin'].unique(maintain_order=True).to_list()
    for name in NAMES:
        q=scores.filter(pl.col('model')==name)
        if q.height:ax.plot(q['wind_bin'].to_list(),q['RMSE'],marker='o',label=LABELS[name],color=COLORS[name])
    ax.set(xlabel='NWP wind speed interval (source scale)',ylabel='Normalized RMSE',title=f'Zone {site}');ax.legend(fontsize=9);ax.tick_params(axis='x',rotation=25);save(fig,out/'wind_errors.png')

def summarize_experiment(folder,ecfg):
    sites=sorted(int(p.parent.name.split('_')[-1]) for p in folder.glob('site_*/complete.json'))
    scores=pl.concat([pl.read_csv(folder/f'site_{s}/metrics.csv') for s in sites]);scores.write_csv(folder/'site_metrics.csv')
    names=[n for n in NAMES if n in scores['model'].to_list()]
    summary=pl.DataFrame([dict(model=n,**pooled_metrics(scores.filter(pl.col('model')==n).to_dicts())) for n in names]);summary.write_csv(folder/'summary.csv')
    wide=pl.concat([pl.read_csv(folder/f'site_{s}/predictions.csv',try_parse_dates=True) for s in sites])
    pair=[dict(site='pooled',**paired_comparison(wide,n,**ecfg['bootstrap'])) for n in names if n!='baseline']
    for s in sites:
        pair.extend(dict(site=str(s),**r) for r in pl.read_csv(folder/f'site_{s}/paired_comparisons.csv').drop('site').to_dicts())
    pl.DataFrame(pair).write_csv(folder/'paired_comparisons.csv')
    resources=[r for s in sites for r in json.loads((folder/f'site_{s}/resources.json').read_text())]
    pl.DataFrame(resources,infer_schema_length=None).write_csv(folder/'resources.csv')
    params=[dict(site=s,**{k:v for k,v in json.loads((folder/f'site_{s}/physics_parameters.json').read_text()).items() if k in ['cut_in','rated','plateau','fit_samples','train_MSE']}) for s in sites]
    pl.DataFrame(params).write_csv(folder/'physics_parameters.csv')
    # Pooled lead/wind scores preserve sample-count weighting and exclude empty bins.
    for kind,key in [('lead','lead'),('wind','wind_bin')]:
        f=pl.concat([pl.read_csv(folder/f'site_{s}/{kind}_metrics.csv') for s in sites])
        result=[]
        for name in names:
            for group in f[key].unique(maintain_order=True).to_list():
                r=f.filter((pl.col('model')==name)&(pl.col(key)==group))
                if r.height:result.append(dict(model=name,**{key:group},**pooled_metrics(r.to_dicts())))
        result_frame=pl.DataFrame(result)
        if kind=='lead':result_frame=result_frame.sort(['model',key])
        result_frame.write_csv(folder/f'{kind}_metrics.csv')
    fig,axs=plt.subplots(1,2,figsize=(12,4.5))
    for ax,key in zip(axs,['MAE','RMSE']):
        vals=[summary.filter(pl.col('model')==n)[key][0] for n in names]
        bars=ax.bar([LABELS[n] for n in names],vals,color=[COLORS[n] for n in names]);ax.bar_label(bars,fmt='%.5f',fontsize=9,padding=3)
        ax.set_ylabel('Pooled normalized '+key);ax.set_ylim(0,max(vals)*1.18);ax.tick_params(axis='x',rotation=22)
    save(fig,folder/'model_comparison.png')
    fig,ax=plt.subplots(figsize=(11,6));ys=np.arange(len(sites)+1)
    for name,offset in [('residual',-.13),('injected',.13)]:
        if name not in names:continue
        q=[next(r for r in pair if r['site']==s and r['candidate']==name) for s in ['pooled']+[str(s) for s in sites]]
        v=np.array([r['delta_RMSE'] for r in q]);lo=np.array([r['delta_RMSE_low'] for r in q]);hi=np.array([r['delta_RMSE_high'] for r in q])
        # CI need not contain point estimate: draw endpoints directly.
        ax.hlines(ys+offset,lo,hi,color=COLORS[name],linewidth=2);ax.scatter(v,ys+offset,color=COLORS[name],label=LABELS[name],s=25)
    ax.axvline(0,color='black',ls='--');ax.set_yticks(ys,['Pooled']+[f'Zone {s}' for s in sites]);ax.invert_yaxis();ax.set_xlabel('RMSE difference vs frozen C (negative = improvement); 95% block bootstrap CI');ax.legend(fontsize=9);ax.grid(alpha=.15);save(fig,folder/'paired_uncertainty.png')
    for kind,key in [('lead','lead'),('wind','wind_bin')]:
        f=pl.read_csv(folder/f'{kind}_metrics.csv');fig,ax=plt.subplots(figsize=(11,5))
        for n in names:
            q=f.filter(pl.col('model')==n)
            if kind=='lead':q=q.sort(key)
            ax.plot(q[key].to_list(),q['RMSE'],label=LABELS[n],color=COLORS[n],marker='o' if kind=='wind' else None)
        ax.set(xlabel='Lead (hours)' if kind=='lead' else 'NWP wind speed interval (source scale)',ylabel='Pooled normalized RMSE');ax.legend(fontsize=9);ax.grid(alpha=.2)
        if kind=='wind':ax.tick_params(axis='x',rotation=25)
        save(fig,folder/f'{kind}_errors.png')
    build_report(folder,sites,summary,scores,pair,params,resources,ecfg)

def build_report(folder,sites,summary,scores,pair,params,resources,ecfg):
    manifest=json.loads((folder/'manifest.json').read_text());eid=manifest['experiment_id']
    def value(n,k):return summary.filter(pl.col('model')==n)[k][0]
    p=next(r for r in pair if r['site']=='pooled' and r['candidate']=='residual')
    delta=p['delta_RMSE'];improvement=-delta/value('baseline','RMSE')*100
    conclusion=('以 RMSE 衡量，混合模型的点估计优于基线' if delta<0 else '以 RMSE 衡量，混合模型的点估计未优于基线')
    certainty=('95% 区间整体低于零，支持在该条件下改善' if p['delta_RMSE_high']<0 else ('95% 区间整体高于零，支持在该条件下变差' if p['delta_RMSE_low']>0 else '95% 区间覆盖零，无法排除测试时间抽样波动'))
    lines=['# 风电物理模型融合实验报告','',f'实验标识：`{eid}`。已完成场站：{sites}。所有数字与图表来自本次真实训练和同样本预测；原有基线结果、latest_runs.json、模型权重和报告均未覆盖。',
    '', '## 摘要与结论','',f'{conclusion}。冻结 LightGBM C 的合并 MAE={value("baseline","MAE"):.6f}、RMSE={value("baseline","RMSE"):.6f}；物理残差混合模型 MAE={value("residual","MAE"):.6f}、RMSE={value("residual","RMSE"):.6f}。RMSE 相对改善为 {improvement:+.3f}%（负值意味着恶化）。配对 RMSE 差（混合−基线）={delta:+.6f}，95% 分块 bootstrap 区间 [{p["delta_RMSE_low"]:+.6f}, {p["delta_RMSE_high"]:+.6f}]；{certainty}。',
    '', f'与此同时，混合模型 MAE 相对降低 {(1-value("residual","MAE")/value("baseline","MAE"))*100:.3f}%，配对 ΔMAE={p["delta_MAE"]:+.6f}，95% 区间 [{p["delta_MAE_low"]:+.6f}, {p["delta_MAE_high"]:+.6f}]。因此结论是 MAE 改善、RMSE 未显示可靠改善，不能笼统声称全面超越原 C。',
    '', '## 1. 等效物理模型与预先固定的方案','', r'由 NWP 的 U100、V100 计算 $v=\sqrt{U100^2+V100^2}$。由于无真实风机参数，以下是场站等效模型，不是经厂家认证的风机功率曲线。风速采用源数据尺度，未推测额定 MW，也未加入无法核实的空气密度、尾流或切出参数。',
    '', r'$$P_{phys}(v)=\begin{cases}0,&v\le v_{ci}\\a\dfrac{v^3-v_{ci}^3}{v_r^3-v_{ci}^3},&v_{ci}<v<v_r\\a,&v\ge v_r\end{cases}$$',
    '', '仅训练集拟合三个参数：切入风速 $v_{ci}$∈[0,6]，额定风速 $v_r$∈[6.5,20]，平台系数 $a$∈[0.3,1]。切入与额定风速网格步长 0.25；每一对网格参数用训练 MSE 的解析最小二乘解拟合 a，并裁剪至边界。总计 25×55=1375 对候选，不用验证或测试标签拟合物理曲线。无切出段，因此极端大风处保持平台，这也是模型限制。',
    '', '比较四种方法：','', '|模型|输入与训练目标|输出|','|---|---|---|','|冻结 LightGBM C|既有 25 维 C，直接预测功率；不重新选择模型|裁剪后的原模型预测|','|纯物理|U100/V100 → 风速；训练集拟合三参数|等效功率曲线|','|Physics-Guided Residual|相同 C，目标 actual−physics；同样的原配置和验证早停|clip(physics + residual, 0, 1)|','|物理特征直接注入|C + physics_prediction，共 26 维，目标 actual|clip(LightGBM(C,physics), 0, 1)|',
    '', '残差本身不裁剪，负残差允许修正物理模型的高估。混合模型没有额外输入物理预测值，只有目标变为残差；注入方案才增加这一维输入。全部方案在查看测试结果前固定，保留所有负面结果。',
    '', '## 2. 数据字段、样本与训练隔离','', '复用 PostgreSQL 18.6 中的 GEFCom2014 Task 15 数据、原 feature_row/build 和 partitions。正式训练样本全部由 PostgreSQL 查询，不直接以 CSV/Parquet 训练。历史功率仍严格早于每日源时钟 00:00 起点，预测未来 1–24 小时。源时区及 NWP 真实到达时间未知，可用性假设与旧实验完全相同。',
    '', '|原始/衍生字段|含义|','|---|---|','|ZONEID / farm_id|场站 1–10，独立建模|','|TIMESTAMP / valid_at|原始无时区时间及未来预报有效时间|','|TARGETVAR / actual|0–1 容量归一化功率；只作监督目标|','|U10,V10,U100,V100|10m/100m NWP 风分量，不是未来实测天气|','|origin,lead|每日预测起点和 1–24 小时提前量|','|C 的物理/时间字段|speed10/100，dir10/100_sin/cos，speed100_cube，shear，hour/year_sin/cos，lead|','|C 的历史字段|lag1/2/24，mean24，std24，min24，max24，trend；全部相对于起点之前|','|physics_prediction|训练集拟合曲线的输出；仅注入方案额外作为输入|',
    '', 'C 共 25 维：4 个原始分量 + 13 个物理/时间特征 + 8 个历史特征。lag1=P(origin−1h)，lag24=P(origin−24h)，过去 24 小时为 [origin−24h,origin−1h]，trend 为窗口端点差。原始缺失功率不填补，所有新模型共用旧模型的完整预测日集合。',
    '', '训练目标区间 (2012-01-01,2013-01-01]；验证 (2013-01-01,2013-07-01]；测试 (2013-07-01,2014-01-01]。物理参数只拟合训练数据。残差标签训练/验证分别扣除同一训练曲线预测，验证仅用于 LightGBM 早停；测试只在所有模型训练完成后用于输出与评分。代码的 fit_suite 不接受测试数据参数。',
    '', 'LightGBM 完全复用原配置：400 棵上限、learning_rate=0.04、num_leaves=15、min_child_samples=40、reg_lambda=1、seed=42、n_jobs=2，验证 30 轮无改善早停。没有按测试结果挑选物理边界、网格或树参数。',
    '', '|场站|训练小时|验证小时|测试小时|切入|额定|平台 a|','|---|---:|---:|---:|---:|---:|---:|']
    for s,par in zip(sites,params):
        meta=json.loads((folder/f'site_{s}/metadata.json').read_text());part=meta['partitions']
        lines.append(f'|{s}|{part["train"]["samples"]}|{part["validation"]["samples"]}|{part["test"]["samples"]}|{par["cut_in"]:.2f}|{par["rated"]:.2f}|{par["plateau"]:.6f}|')
    lines+=['', f'本次 {sum(p["cut_in"]==0 for p in params)}/{len(params)} 个场站的切入参数拟合到预设下界 0，不能解释为真实风机零风速启动。这反映出简单预报风速—聚合功率映射对切入段的辨识有限；等效参数同时吸收 NWP 偏差、场站聚合和运行状态。边界命中与无切出段都是应保留的局限，本次未据测试结果修改范围。','', '## 3. 同样本模型指标','', '|模型|MAE=nMAE|RMSE=nRMSE|测试小时|','|---|---:|---:|---:|']
    for r in summary.to_dicts():lines.append(f'|{LABELS[r["model"]]}|{r["MAE"]:.6f}|{r["RMSE"]:.6f}|{r["n"]}|')
    lines+=['', '![整体模型比较](model_comparison.png)','', '**图1. 按实际小时数加权的合并指标。** 功率已容量归一化，nMAE/MAE、nRMSE/RMSE 数值相同。RMSE 先合并平方误差再开根。基线逐样本与既有导出核对，目标键、标签和预测必须一致，否则实验停止。','', '|场站|冻结 C RMSE|纯物理 RMSE|混合 RMSE|注入 RMSE|混合−C|','|---|---:|---:|---:|---:|---:|']
    for s in sites:
        r={r['model']:r for r in scores.filter(pl.col('site')==s).to_dicts()}
        lines.append('|'+str(s)+'|'+'|'.join(f'{r[n]["RMSE"]:.6f}' for n in NAMES if n in r)+f'|{r["residual"]["RMSE"]-r["baseline"]["RMSE"]:+.6f}|')
    lines+=['', '|场站|冻结 C MAE|纯物理 MAE|混合 MAE|注入 MAE|','|---|---:|---:|---:|---:|']
    for site in sites:
        rows={r['model']:r for r in scores.filter(pl.col('site')==site).to_dicts()}
        lines.append('|'+str(site)+'|'+'|'.join(f'{rows[name]["MAE"]:.6f}' for name in NAMES if name in rows)+'|')
    lines+=['', '## 4. 配对比较与不确定性','', '采用 2000 次、7 个连续日为块的 circular block bootstrap，随机种子 42。每次抽取同一批日历日与其全部小时、全部场站，保持模型间配对和场站间同期相关；缺失日不压缩时间轴。置信区间是差值 bootstrap 分布的 2.5%/97.5% 分位数，负差值代表新模型更优。','', '这是对固定已训练模型和现有十站条件下的测试时间抽样不确定性估计，不包含重新训练、其他年份、站点总体抽样或多种块长度的敏感性。未进行多重比较校正；额外注入方案和逐站结果为探索性比较。bootstrap 改善比例不是正式 p 值。','', '|范围/模型|ΔMAE|95% 区间|ΔRMSE|95% 区间|bootstrap 中 RMSE 更优比例|','|---|---:|---|---:|---|---:|']
    for r in pair:
        lines.append(f'|{r["site"]} / {LABELS[r["candidate"]]}|{r["delta_MAE"]:+.6f}|[{r["delta_MAE_low"]:+.6f}, {r["delta_MAE_high"]:+.6f}]|{r["delta_RMSE"]:+.6f}|[{r["delta_RMSE_low"]:+.6f}, {r["delta_RMSE_high"]:+.6f}]|{r["bootstrap_fraction_RMSE_better"]:.3f}|')
    lines+=['', '![配对不确定性](paired_uncertainty.png)','', '**图2. 混合/注入相对冻结 C 的 RMSE 差及 95% 区间。** 零线左侧为改善，跨零不能排除测试时间抽样波动；不选择性删除不利站点。','', '## 5. 时效及风速区间表现','', '![分时效误差](lead_errors.png)','', '**图3. 1–24 小时的合并 RMSE，使用相同样本。**','', '![分风速误差](wind_errors.png)','', '**图4. 按 NWP 100m 风速分箱的合并 RMSE。** 固定区间 [0,3)、[3,6)、[6,9)、[9,12)、[12,15)、[15,20)、[20,∞)。不是按真实实测风速分组；各箱样本量见下表，空箱不虚构误差。','', '|NWP 风速区间|样本数|冻结 C RMSE|纯物理 RMSE|混合 RMSE|注入 RMSE|','|---|---:|---:|---:|---:|---:|']
    wind=pl.read_csv(folder/'wind_metrics.csv')
    for label in wind['wind_bin'].unique(maintain_order=True).to_list():
        q={r['model']:r for r in wind.filter(pl.col('wind_bin')==label).to_dicts()}
        lines.append('|'+label+'|'+str(q['baseline']['n'])+'|'+'|'.join(f'{q[n]["RMSE"]:.6f}' for n in NAMES if n in q)+'|')
    lines+=['', '## 6. 参数量、训练时间与推理成本','', '旧模型历史训练耗时没有记录，不能补造。本次用完全相同配置对 C 做一次受控重训，仅用于计时，不替换冻结基线；其预测差异记录于资源 CSV。训练耗时不包含 PostgreSQL 查询/共享特征构造/文件与数据库写入；混合、注入耗时包含各自的物理拟合时间（实际共享拟合一次，按独立部署成本计入）。','', '树模型参数量定义为预测实际使用的树的叶输出数+分裂阈值数，物理模型另加 3 个标量；不把这一口径称作神经网络参数量，未计离散拓扑、特征 ID 和序列化开销。另报树数及 joblib 文件大小。推理为相同测试批次预热 3 次后 20 次中位数，输入是已经构好的 C 矩阵；含物理风速计算/曲线及最终裁剪，不含查询与特征构造。每 24 小时的数值是批量吞吐折算，不是单日独立请求延迟。','', f'环境：{manifest["platform"]}；Python {manifest["python"]}，LightGBM 两线程。同机顺序计时受系统负载影响，不能把微小差异解释为稳定性能优势。','', '|场站/模型|树数|标量参数|训练秒|批量推理 ms|折算每 24h ms|文件 KiB|','|---|---:|---:|---:|---:|---:|---:|']
    for r in resources:lines.append(f'|{r["site"]} / {LABELS[r["model"]]}|{r["active_trees"]}|{r["scalar_parameters"]}|{r["train_seconds"]:.4f}|{r["inference_batch_seconds"]*1000:.4f}|{r["inference_ms_per_24h"]:.4f}|{r["artifact_bytes"]/1024:.1f}|')
    lines+=['', '十站成本汇总（训练秒数及标量参数数求和；推理为各站批次中位数相加，不是联合批次实测）：','', '|模型|标量参数合计|训练秒合计|各站批量推理 ms 之和|','|---|---:|---:|---:|']
    for name in NAMES:
        rows=[r for r in resources if r['model']==name]
        if rows:lines.append(f'|{LABELS[name]}|{sum(r["scalar_parameters"] for r in rows)}|{sum(r["train_seconds"] for r in rows):.4f}|{sum(r["inference_batch_seconds"] for r in rows)*1000:.4f}|')
    refit_diffs=[r['test_refit_max_abs_diff'] for r in resources if r['model']=='baseline']
    lines+=['',f'受控 C 重训相对冻结 C 的全测试最大绝对预测差：{max(refit_diffs):.3g}。这次受控重训未替换冻结基线。']
    lines+=['', '## 7. 逐站物理曲线与预测诊断','', '所有物理散点图仅展示训练集，红色曲线参数也只拟合训练集；预测曲线固定展示前 168 个有效测试小时，而非选择误差最小窗口。完整指标覆盖全部测试样本。']
    for s in sites:
        lines+=['',f'### Zone {s}','']
        for file,cap in [('physics_curve.png','训练集风速—功率密度与等效物理曲线'),('prediction_comparison.png','冻结 C、纯物理、混合及注入预测对比'),('lead_errors.png','逐提前量误差'),('wind_errors.png','逐 NWP 风速区间误差')]:
            lines += [f'![Zone {s} {cap}](site_{s}/{file})','',f'**Zone {s}：{cap}。**','']
    receipts=[folder/k for k in ['database_acceptance.json','legacy_preservation.json','execution_validation.json']]
    if all(path.exists() for path in receipts):
        db,legacy,validation=[json.loads(path.read_text()) for path in receipts]
        lines+=['',f'验收结果：{validation["tests"]}；新增 {db["verified_model_runs"]} 个实验运行和 {db["verified_predictions"]:,} 条预测逐条核对数据库与 CSV 一致；旧目录检查 {legacy["legacy_files_checked"]} 个文件，变动 {len(legacy["changed_files"])} 个。database_acceptance.json、legacy_preservation.json、execution_validation.json 保存检查证据。']
        if validation.get('database_restored_closed'):lines+=['', '数据库已恢复关闭。']
    lines+=['', '## 8. 可追溯性与保留范围','', f'独立输出目录 `{folder}`。manifest.json 固定配置；每站包含 physics_parameters.json、metadata.json、metrics.csv、predictions.csv、lead_metrics.csv、wind_metrics.csv、paired_comparisons.csv、resources.json、四个 joblib 模型和四张图。新运行 UUID、预测和配置写入原 PostgreSQL model_runs/predictions，source_baseline_id 指向旧基线。旧 latest_runs.json、指标 CSV、权重、报告保持原样。','', '实验先完成 Zone 1 的代码/样本一致性检查，再以同一配置续跑 1–10；续跑不重新训练已完成站点。实验中没有根据测试结果修改物理边界或模型超参数。数据库在实验期间临时启动，整理完成后恢复关闭。','', '物理曲线是有界等效近似，没有真实切出、空气密度、尾流、停机或限电状态；残差学习未增加约束保证模型遵守单调性，也不保证域外外推仍符合物理。结果不能直接称为已验证的生产级物理模型。']
    (folder/'report.md').write_text('\n'.join(lines),encoding='utf-8')

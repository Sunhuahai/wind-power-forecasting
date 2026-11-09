"""Create a figure-rich Obsidian report from frozen experiment outputs; no DB needed."""
from pathlib import Path
import json
import csv
import shutil
import re
import numpy as np
import polars as pl
import joblib
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from windpower.features.build import A,B,C
ROOT=Path.cwd()
OUT=Path('/private/tmp/windpower-paper-report');OUT.mkdir(exist_ok=True)
FIG=OUT/'论文图表';FIG.mkdir(exist_ok=True)
BASE='实验结果_2026-11-09/artifacts'
MODELS=['persistence_C','ridge_C','lightgbm_A','lightgbm_B','lightgbm_C']
LABELS=['Persistence','Ridge (C)','LightGBM A','LightGBM B','LightGBM C']
COLORS=['#8c8c8c','#e5a23c','#518ac1','#57a78a','#bd5973']
plt.rcParams.update({'font.size':11,'axes.spines.top':False,'axes.spines.right':False,'figure.facecolor':'white'})
metrics=pl.read_csv('artifacts/all_sites_metrics.csv')
summary=pl.read_csv('artifacts/all_sites_summary.csv')
lead=pl.concat([pl.read_csv(f'artifacts/site_{i}/lead_metrics.csv') for i in range(1,11)])
meta={};pred={}
for site in range(1,11):
 ids=json.loads(Path(f'artifacts/site_{site}/latest_runs.json').read_text())
 meta[site]=[joblib.load(f'artifacts/site_{site}/{i}.joblib')['metadata'] for i in ids]
 pred[site]={m:pl.read_csv(f'artifacts/site_{site}/{i}_predictions.csv',try_parse_dates=True) for m,i in zip(MODELS,ids)}
def save(fig,name):
 fig.savefig(FIG/name,dpi=180,bbox_inches='tight');plt.close(fig)
def val(model,key):return summary.filter(pl.col('model')==model)[key][0]
fig,axs=plt.subplots(1,2,figsize=(12,4.6))
for ax,key in zip(axs,['MAE','RMSE']):
 bars=ax.bar(LABELS,[val(m,key) for m in MODELS],color=COLORS)
 ax.bar_label(bars,fmt='%.4f',padding=3,fontsize=9)
 ax.set_ylim(0,max(val(m,key) for m in MODELS)*1.16);ax.set_ylabel(f'Pooled normalized {key}');ax.tick_params(axis='x',rotation=24)
fig.tight_layout();save(fig,'01_整体模型比较.png')
fig,axs=plt.subplots(1,2,figsize=(13,4.5))
for m,label,color in zip(MODELS,LABELS,COLORS):
 rows=[]
 for h in range(1,25):
  p=lead.filter((pl.col('model')==m)&(pl.col('lead')==h));n=p['n'].to_numpy()
  rows.append((np.average(p['MAE'].to_numpy(),weights=n),np.sqrt(np.average(p['RMSE'].to_numpy()**2,weights=n))))
 for j,ax in enumerate(axs):ax.plot(range(1,25),np.array(rows)[:,j],label=label,color=color,linewidth=2)
for ax,key in zip(axs,['MAE','RMSE']):ax.set(xlabel='Lead time (hours)',ylabel=f'Pooled normalized {key}',xticks=[1,4,8,12,16,20,24]);ax.grid(alpha=.2)
axs[1].legend(fontsize=9);fig.tight_layout();save(fig,'02_整体分时效误差.png')
mat=np.array([[metrics.filter((pl.col('site')==s)&(pl.col('model')==m))['RMSE'][0] for m in MODELS] for s in range(1,11)])
fig,ax=plt.subplots(figsize=(10,7));im=ax.imshow(mat,cmap='YlGnBu',aspect='auto');ax.set_xticks(range(5),LABELS,rotation=20);ax.set_yticks(range(10),[f'Zone {s}' for s in range(1,11)])
for i in range(10):
 for j in range(5):ax.text(j,i,f'{mat[i,j]:.4f}',ha='center',va='center',color='white' if mat[i,j]>.28 else 'black')
fig.colorbar(im,ax=ax,label='Normalized RMSE');fig.tight_layout();save(fig,'03_各场站误差矩阵.png')
fig,ax=plt.subplots(figsize=(11,4.8));x=np.arange(10);d1=mat[:,3]-mat[:,2];d2=mat[:,4]-mat[:,3]
ax.bar(x-.19,d1,.38,label='B minus A: physical features',color=COLORS[3]);ax.bar(x+.19,d2,.38,label='C minus B: power history',color=COLORS[4]);ax.axhline(0,color='black',linewidth=.8);ax.set_xticks(x,[str(s) for s in range(1,11)]);ax.set(xlabel='Wind farm (Zone)',ylabel='Change in normalized RMSE');ax.legend();ax.grid(axis='y',alpha=.2);fig.tight_layout();save(fig,'04_逐站特征消融.png')
fig,axs=plt.subplots(5,2,figsize=(16,15),sharey=True)
for s,ax in enumerate(axs.flat,1):
 f=pred[s]['lightgbm_C'].head(168)
 ax.plot(f['valid_at'],f['actual'],label='Actual',color='#222222',linewidth=1.2)
 ax.plot(f['valid_at'],f['prediction'],label='LightGBM C',color=COLORS[4],linewidth=1.2)
 ax.set_title(f'Zone {s}');ax.set_ylim(-.03,1.03);ax.tick_params(axis='x',rotation=20,labelsize=8);ax.grid(alpha=.15)
axs[0,0].legend();fig.supylabel('Power / nominal capacity');fig.tight_layout();save(fig,'05_十站预测曲线.png')
fig,axs=plt.subplots(1,2,figsize=(11,4.5))
f=pred[1]['lightgbm_C'];y=f['actual'].to_numpy();p=f['prediction'].to_numpy()
axs[0].hexbin(y,p,gridsize=35,mincnt=1,cmap='Blues');axs[0].plot([0,1],[0,1],ls='--',color='#c45d5d');axs[0].set(xlabel='Actual normalized power',ylabel='Predicted normalized power',title='Zone 1: all test samples')
axs[1].hist(p-y,bins=50,color=COLORS[4],edgecolor='white');axs[1].axvline(0,ls='--',color='black');axs[1].set(xlabel='Prediction minus actual',ylabel='Hourly samples',title='Zone 1: LightGBM C residuals');fig.tight_layout();save(fig,'06_单站散点与残差.png')
L=['# 风电功率预测：GEFCom2014 十场站实验报告','', '> 本文直接展示真实实验的图表与数据字段。全部指标由已完成的训练和独立测试产生；未重新训练、未根据测试结果调参。数据库已关闭，本次整理读取冻结的 CSV、Parquet 与模型元数据。','', '## 摘要','', '采用官方 GEFCom2014-Wind V2 Task 15 数据，对 10 个风电场实施每日一次、未来 24 小时的小时级功率预测。先完成 Zone 1，再以同一固定配置扩展全部场站。比较 Persistence、Ridge 以及 LightGBM 的原始 NWP（A）、物理特征（B）、物理与历史功率（C）三种输入。十站合并测试中，LightGBM C 的 MAE 为 0.123454、RMSE 为 0.170454；Persistence 的 RMSE 为 0.348900。历史功率并非对每个场站都改善结果。','', '## 1. 数据来源与原始字段','', '原始来源为 IEA Wind 基准页面提供的官方 GEFCom2014 ZIP，实际下载大小为 126,360,077 字节。使用内层 `GEFCom2014-W_V2.zip`：历史文件 `Task15_W_ZoneN.csv`、未来预报文件 `TaskExpVars15_W_ZoneN.csv` 和十二月标签文件 `solution15_W.csv`。未复制其他预测仓库，未使用 ERA5 或合成补值。','', '|原始字段|含义|用途与质量规则|','|---|---|---|','|ZONEID|风电场编号，1–10|映射 farm_id，按场站独立建模，不作为单站输入|','|TIMESTAMP|该小时的源时间字符串，例如 `20120101 1:00`|解析为无时区时间；保留原文，不擅自转换 UTC|','|TARGETVAR|相对额定容量归一化的真实功率|监督学习标签；合法范围 0–1；NA 保留 NULL，不转 MW|','|U10|10m 高度风预报的 u 分量|原始 NWP 输入；不等于目标时刻实测天气|','|V10|10m 高度风预报的 v 分量|原始 NWP 输入|','|U100|100m 高度风预报的 u 分量|原始 NWP 输入|','|V100|100m 高度风预报的 v 分量|原始 NWP 输入|','', 'u/v 为纬向/经向风矢量分量；风速沿用源字段数值尺度，本项目未另行换算单位。所有数据保留原生 1 小时分辨率。历史文件包含功率和 NWP；未来文件不含功率，十二月标签来自官方答案文件，不是人为填补。','', '### 1.1 数据覆盖与清洗','', '每站原始记录 17,544 小时，共 175,440 条功率记录和同量 NWP 记录，范围为 2012-01-01 01:00 至 2014-01-01 00:00。时间轴无缺小时、无重复键，四个 NWP 字段无缺失/非有限值；真实功率共 175 个缺失值。字段缺失与时间轴缺小时是不同问题。','', '|场站|原始小时|缺失功率|训练小时|验证小时|测试小时|测试完整日|','|---|---:|---:|---:|---:|---:|---:|']
for s in range(1,11):
 q=json.loads(Path(f'artifacts/site_{s}/quality_source.json').read_text());n=pred[s]['lightgbm_C'].height;c=meta[s][-1]
 L.append(f'|{s}|17544|{q["field_issues"].get("power:missing",0)}|{c["train_samples"]}|{c["validation_samples"]}|{n}|{n//24}|')
L+=['', '不插补缺失功率。任何预测日若缺少某个目标标签、NWP 或起点前 24 小时功率，就从五组模型共同样本中排除；因此 A/B 虽不使用历史功率，也采用与 C 完全一致的样本集合。最初历史不足的起点也被排除。','', '## 2. 预测任务、训练样本与防泄漏','', r'设预测起点为源时钟每日 00:00 的 $o$，提前量 $h\in\{1,\ldots,24\}$，目标时刻 $t=o+h$。每个训练样本对应一个“场站—起点—目标小时”；每日每个模型输出 24 个数值。目标恰为次日 00:00 时，它属于前一日起点的第 24 小时。','', '|样本字段|作用|是否作为模型输入|','|---|---|---|','|farm_id（数据库）|场站键，查询时限定场站|否；各站独立训练|','|origin|每日预测起点|不直接输入；决定历史可用性|','|valid_at|未来目标小时|不直接输入；构造周期特征|','|lead|目标与起点相差的小时数 1–24|B/C 输入|','|actual|目标时刻真实功率|仅作标签与评价，绝不输入模型|','|u10/v10/u100/v100|目标时刻对应的 NWP|A/B/C 输入|','|其余物理及历史字段|下面两表列出的衍生特征|按特征组选择|','', '### 2.1 原始与物理特征（A：4 维，B：17 维）','', '|特征名|定义/含义|所属组|','|---|---|---|','|u10, v10, u100, v100|原始四个 NWP 分量|A/B/C|',r'|speed10, speed100|$s_z=\sqrt{u_z^2+v_z^2}$|B/C|',r'|dir10_sin, dir10_cos|$v_{10}/\max(s_{10},10^{-8})$ 与 $u_{10}/\max(s_{10},10^{-8})$|B/C|','|dir100_sin, dir100_cos|100m 同样的风向周期编码|B/C|','|speed100_cube|$s_{100}^{3}$，与风能物理关系相关的非线性特征|B/C|',r'|shear|$\ln[\max(s_{100},10^{-6})/\max(s_{10},10^{-6})]/\ln(10)$|B/C|',r'|hour_sin, hour_cos|目标小时的 $\sin(2\pi\,hour/24)$、$\cos(2\pi\,hour/24)$|B/C|','|year_sin, year_cos|目标年内日序的周期编码，周期 365.25 日|B/C|','|lead|1–24 小时提前量|B/C|','', '方向编码表示风矢量指向，与气象学“风从哪个方向来”的角度定义不同；静风的两个方向分量为零。这里没有把未来实测风速当作输入。','', '### 2.2 历史功率特征（C：25 维）','', '|特征名|精确定义|可用性|','|---|---|---|','|lag1|$P(o-1h)$|起点前 1 小时|','|lag2|$P(o-2h)$|起点前 2 小时|','|lag24|$P(o-24h)$|起点前 24 小时|',r'|mean24|$P(o-24h),\ldots,P(o-1h)$ 的均值|24 个已发生小时|','|std24|同一窗口的总体标准差（ddof=0）|同上|','|min24, max24|同一窗口最小值、最大值|同上|','|trend|$P(o-1h)-P(o-24h)$，23 小时端点变化量|同上|','', 'C 是 B 的 17 个字段加上上述 8 个字段。这里的 lag 相对于预测起点，不是相对于未来目标时刻；同一天的 24 个目标共享同一历史窗口。连起点 00:00 本身的功率也不读取。Persistence 只用 lag1，Ridge 使用 C 的 25 维，LightGBM 分别使用 A/B/C。','', '### 2.3 实际训练行示例','', '下表读取实际从 PostgreSQL 生成的 Zone 1 训练样本导出，仅摘取部分列。actual 是监督目标，不属于输入 X。','', '|origin|valid_at|lead|u10|v10|speed100|lag1|actual（标签）|','|---|---|---:|---:|---:|---:|---:|---:|']
f=pl.read_parquet('artifacts/site_1/train_samples.parquet').head(3)
for r in f.iter_rows(named=True):L.append('|'+ '|'.join([str(r['origin']),str(r['valid_at']),str(r['lead'])]+[f'{r[k]:.6f}' for k in ['u10','v10','speed100','lag1','actual']])+'|')
L+=['', '### 2.4 固定时间划分与模型选择','', '|用途|目标时间区间（左开右闭）|作用|','|---|---|---|','|训练|2012-01-01 至 2013-01-01|拟合模型、Ridge 标准化参数|','|验证|2013-01-01 至 2013-07-01|选择 Ridge alpha、LightGBM 早停|','|测试|2013-07-01 至 2014-01-01|模型冻结后独立预测与评价|','', '目标标签不得跨越划分边界。逐日滚动回放时，之前测试日期已发生的功率可以作为后续起点历史，但不用于重新拟合。Ridge 的 StandardScaler 仅拟合训练集；alpha 候选为 0.1、1、10、100，按验证 MSE 选取。LightGBM 上限 400 棵树，learning_rate=0.04、num_leaves=15、min_child_samples=40、reg_lambda=1、seed=42、n_jobs=2，验证集连续 30 轮无改善则早停。所有预测统一裁剪至 [0,1]。','', '|场站|Ridge alpha|LightGBM A 最佳轮数|B 最佳轮数|C 最佳轮数|','|---|---:|---:|---:|---:|']
for s in range(1,11):L.append(f'|{s}|{meta[s][1]["selected"]["alpha"]}|{meta[s][2]["selected"]["best_iteration"]}|{meta[s][3]["selected"]["best_iteration"]}|{meta[s][4]["selected"]["best_iteration"]}|')
L+=['', '## 3. 整体模型对比','', r'$$\mathrm{MAE}=\frac{1}{N}\sum_i|\hat y_i-y_i|,\qquad \mathrm{RMSE}=\sqrt{\frac{1}{N}\sum_i(\hat y_i-y_i)^2}.$$','', '功率已按容量归一化，所以 nMAE=MAE、nRMSE=RMSE；乘 100 后可读为额定容量百分比，不能读作 MW。跨站先按实际小时样本数合并绝对/平方误差，再计算 MAE/RMSE，不对各站 RMSE 简单算术平均。每个模型使用相同的 42,432 个测试小时。','', '|模型|MAE / nMAE|RMSE / nRMSE|测试小时|','|---|---:|---:|---:|']
for m,label in zip(MODELS,LABELS):L.append(f'|{label}|{val(m,"MAE"):.6f}|{val(m,"RMSE"):.6f}|42432|')
L+=['', '![图1 整体模型对比](论文图表/01_整体模型比较.png)','', '**图1. 十站合并测试的 MAE 与 RMSE。** C 的 RMSE 为 0.170454，较 Persistence 降低约 '+f'{(1-val("lightgbm_C","RMSE")/val("persistence_C","RMSE"))*100:.2f}'+'%。该比较仅适用于本次固定划分与可用性假设，不是生产效果承诺。','', '## 4. 各场站误差与特征消融','', '![图2 各场站误差矩阵](论文图表/03_各场站误差矩阵.png)','', '**图2. 各场站、各模型的测试 RMSE。** 每站内部五组模型共用同一测试样本；不同场站的有效测试天数不同，不把站点之间的误差差异直接解释为场站物理难度差异。','', '|场站|Persistence RMSE|Ridge RMSE|LGBM A|LGBM B|LGBM C|','|---|---:|---:|---:|---:|---:|']
for s in range(1,11):L.append('|'+str(s)+'|'+'|'.join(f'{v:.6f}' for v in mat[s-1])+'|')
L+=['', '![图3 逐站特征消融](论文图表/04_逐站特征消融.png)','', '**图3. B−A 与 C−B 的逐站 RMSE 差。** 负数代表加入该组特征后改善。整体 B 相对 A 改善 0.001622，C 相对 B 改善 0.002655；但 Zone 1、3、4、10 的 C 比 B 更差，Zone 7、8 的 B 比 A 更差。说明特征收益具有站点差异，不能仅凭整体均值认定所有新增特征都有效。','', '## 5. 提前量误差','', '![图4 整体分时效误差](论文图表/02_整体分时效误差.png)','', '**图4. 1–24 小时提前量的十站合并 MAE/RMSE。** 每个提前量分别汇总对应测试样本；持续性模型的误差随提前量明显增大。曲线来自实际逐时效评分，不是插值的示意曲线。各站完整时效曲线见附录。','', '## 6. 预测时间曲线与误差分布','', '![图5 十站预测曲线](论文图表/05_十站预测曲线.png)','', '**图5. 各站 LightGBM C 的前 168 个有效测试小时。** 黑线为真实值，红线为预测值，纵轴统一为归一化功率。窗口按时间顺序选取，不挑选效果最好的片段；若期间有整日剔除，横轴保留真实时间间隔。C 并非每站最优，本图固定使用 C，避免按测试结果更换模型。','', '![图6 Zone 1 散点与残差](论文图表/06_单站散点与残差.png)','', '**图6. Zone 1 的 LightGBM C 全测试样本散点密度及残差分布。** 左图虚线为理想预测线；右图残差定义为预测−真实，正值为高估。散点图和直方图覆盖全部 4,104 个测试小时，补充短时间窗口曲线无法体现的整体误差。','', '## 7. 特征重要性与解释边界','', f'![图7 Zone 1 C 特征重要性]({BASE}/site_1/lightgbm_C_importance.png)','', '**图7. Zone 1 LightGBM C 的前 15 个 split gain 特征。** 100m 风速在该模型中占主导，历史功率也被使用，但“被使用”不等于“改善独立测试指标”。gain 受特征相关性和树的划分方式影响，不是因果效应；不同模型的重要性绝对数值也不直接比较。十站 A/B/C 的全部重要性图均内嵌在附录。','', '## 8. 数据管理、可重复性与局限','', '六张表分别为 wind_farms、power_observations、nwp_forecasts、dataset_splits、model_runs、predictions。观测以场站+观测时间唯一，NWP 以场站+有效时间唯一，预测以运行+起点+有效时间唯一。原始时间字符串、字段质量标记及 NWP 可用性假设保留；正式训练数据均由 PostgreSQL 查询生成，本文整理阶段仅使用那些已生成的冻结结果。','', '实际完成：10 站官方数据导入，50 组最新模型，212,160 条最新模型测试预测；保留最初 Zone 1 实验后，数据库共 55 个模型运行及 232,680 条预测。重复导入/预测后六表行数不变。7 项自动化测试通过；新 Python 环境与空 PostgreSQL 数据库重跑 Zone 1，MAE、RMSE、样本数量与原实验逐项相同。数据库已按要求关闭。','', '- **时间可用性假设：** 源时区未知。每日源时间 00:00 及未来 1–24 小时 NWP 已到达，是明确的竞赛回放假设；实际 issued_at/到达日志无法核实，数据库保持 NULL。历史观测真实传输延迟同样未知。','- **数据覆盖限制：** 仅两年、10 个场站；缺失值不插补而剔除完整日，未验证缺失机制是否随机，报告不包含置信区间或显著性检验。','- **业务限制：** 没有真实装机 MW、限电/停机状态或多批次 NWP 版本，企业接入需要真实时区、到达时间和版本管理。','- **运行环境：** 本次实际数据库为原生 PostgreSQL 18.6。Docker Compose 已配置官方 postgres:18.6，但本机无 Docker，未实际验收该启动路径。','', '## 附录：十场站完整结果图','', '以下直接嵌入每站的模型比较、五模型预测曲线、分时效误差及三组 LightGBM 重要性图。预测曲线使用各站前 168 个有效测试小时；数值评价使用该站全部独立测试样本。']
for s in range(1,11):
 L += ['',f'### A{s}. Zone {s}','']
 for filename,caption in [('model_comparison.png','五模型整体 RMSE 比较'),('prediction_curve.png','真实功率与五模型预测时间曲线'),('lead_errors.png','1–24 小时 RMSE 曲线'),('lightgbm_A_importance.png','A：原始 NWP 的 gain 重要性'),('lightgbm_B_importance.png','B：物理特征的 gain 重要性'),('lightgbm_C_importance.png','C：物理与历史功率的 gain 重要性')]:
  L += [f'![Zone {s} {caption}]({BASE}/site_{s}/{filename})','',f'**Zone {s} · {caption}。**','']
text='\n'.join(L)
(OUT/'风电功率预测_实验报告.md').write_text(text,encoding='utf-8')
# Verify every image resolves in the intended destination layout.
links=re.findall(r'!\[[^]]*\]\(([^)]+)\)',text)
for link in links:
 p=OUT/link if link.startswith('论文图表/') else ROOT/'artifacts'/link.split('/artifacts/',1)[1]
 assert p.is_file(),p
print(f'Generated {len(links)} embedded figures, {len(text)} characters; all image targets verified.')

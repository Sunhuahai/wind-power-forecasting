#!/usr/bin/env python
"""生成 F01 风电预测结果可视化仪表盘。

单文件 HTML，全部样式/脚本内联，不依赖外部 CDN（国内网络可直接访问）。
数据只包含聚合指标与日级统计，不含任何逐行原始 SCADA（数据政策见
docs/周进展/2026-W47（11-16～11-22）/02 规划 §4.3）。

用法：
    uv run python scripts/build_dashboard.py
    # 输出 artifacts/dashboard/index.html
"""
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from windpower.db.core import engine

OUT = Path('artifacts/dashboard/index.html')
MODEL_COLORS = {'persistence': '#f4b942', 'seasonal': '#a78bfa',
                'ridge': '#2dd4a7', 'lightgbm': '#5aa9ff'}


def _git_commit() -> str:
    try:
        return subprocess.run(['git', 'rev-parse', '--short', 'HEAD'],
                               capture_output=True, text=True,
                               check=True).stdout.strip()
    except Exception:
        return 'unknown'


def daily_power() -> list[dict]:
    """F01 日级功率包络与负功率占比：367 行聚合，非逐行数据。"""
    with engine().connect() as c:
        rows = c.execute(text("""
            SELECT observed_at::date AS d,
                   avg(grid_active_power_kw) AS mean_kw,
                   max(grid_active_power_kw) AS max_kw,
                   min(grid_active_power_kw) AS min_kw,
                   count(*) FILTER (WHERE grid_active_power_kw < 0) AS neg_n,
                   count(*) AS n,
                   avg(nacelle_wind_speed_ms) AS wind_ms
              FROM turbine_scada_10m GROUP BY 1 ORDER BY 1""")).mappings().all()
    return [dict(date=str(r['d']), mean=round(float(r['mean_kw']), 1),
                 max=round(float(r['max_kw']), 1), min=round(float(r['min_kw']), 1),
                 neg_share=round(100.0 * r['neg_n'] / r['n'], 1),
                 wind=round(float(r['wind_ms']), 2)) for r in rows]


def build_payload() -> dict:
    summary = json.loads(Path('artifacts/f01/summary.json').read_text())
    quality = json.loads(Path('artifacts/f01/quality_report.json').read_text())
    manifest = json.loads(Path('artifacts/f01/run_manifest.json').read_text())
    import csv

    def read_csv(path):
        with open(path) as fh:
            return list(csv.DictReader(fh))

    per_lead = read_csv('artifacts/f01/per_lead.csv')
    per_day = read_csv('artifacts/f01/per_day.csv')
    models = summary['models']

    test = None
    if Path('artifacts/f01/test_summary.json').exists():
        ts = json.loads(Path('artifacts/f01/test_summary.json').read_text())
        test = dict(
            best=min(ts['models'], key=lambda m: ts['models'][m]['RMSE']),
            bounds=ts['coverage']['splits']['test'],
            models={m: dict(MAE=round(v['MAE'], 1), RMSE=round(v['RMSE'], 1),
                            Bias=round(v['Bias'], 1), n=v['n'],
                            val_rmse=round(ts['gate']['validation_reference_rmse'][m], 1),
                            colors=MODEL_COLORS.get(m, '#888'))
                    for m, v in ts['models'].items()},
            leads=[dict(model=r['model'], lead=int(r['lead_minutes']),
                        RMSE=float(r['RMSE'])) for r in read_csv('artifacts/f01/test_per_lead.csv')],
        )
    return dict(
        generated_at=datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds'),
        commit=_git_commit(),
        dataset_sha=summary['dataset']['file_sha256'][:16],
        time_range=dict(start=quality['time_axis']['start'],
                        end=quality['time_axis']['end']),
        kpi=dict(
            rows=quality['file']['rows'],
            grid_complete=round(100 * (1 - quality['time_axis']['missing_grid_points']
                                        / quality['time_axis']['expected_grid_points']), 2),
            coverage=round(100 * summary['coverage']['coverage_share'], 1),
            neg_share=round(100 * quality['label_candidate']['grid_active_power_kw']['negative_share'], 1),
            neg_count=quality['label_candidate']['grid_active_power_kw']['negative_count'],
            gate=summary['gate']['status'],
            test_status=summary['test_set']['status'],
            val_rmse=round(min(m['RMSE'] for m in models.values()), 1),
            best_model=min(models, key=lambda m: models[m]['RMSE']),
            rated_note='额定功率未核实（GW191/5270 仅为型号字符串）',
            test_rmse=(round(min(m['RMSE'] for m in test['models'].values()), 1)
                       if test else None),
            test_best=(test['best'] if test else None),
        ),
        test=test,
        splits=summary['coverage']['splits'],
        models={m: dict(MAE=round(v['MAE'], 1), RMSE=round(v['RMSE'], 1),
                        Bias=round(v['Bias'], 1), n=v['n'],
                        runtime=v['runtime'],
                        colors=MODEL_COLORS.get(m, '#888')) for m, v in models.items()},
        leads=[dict(model=r['model'], lead=int(r['lead_minutes']),
                    MAE=float(r['MAE']), RMSE=float(r['RMSE'])) for r in per_lead],
        days=[dict(model=r['model'], date=r['date'],
                   RMSE=float(r['RMSE']), n_issues=int(r['n_issues'])) for r in per_day],
        best=dict(
            name=manifest.get('snapshot_id', '')[:8],
            model=min(models, key=lambda m: models[m]['RMSE']),
            groups=models[min(models, key=lambda m: models[m]['RMSE'])],
        ),
        audit=dict(
            time_axis=quality['time_axis'],
            row_flags=quality['row_flag_hits'],
            wind=quality['wind_speed'],
            label=quality['label_candidate']['grid_active_power_kw'],
            identity=quality['identity'],
            rules_note='全部规则预注册、只标记不删改；负功率原值入库不截零。',
        ),
        thresholds=summary['thresholds'],
        daily=daily_power(),
        ramp=models[min(models, key=lambda m: models[m]['RMSE'])]['ramp'],
    )


HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>F01 风电功率预测 · 结果仪表盘</title>
<style>
:root{--bg:#0e1420;--panel:#161e2e;--panel2:#1b2438;--line:#263248;--txt:#dbe4f0;
--muted:#8296b3;--accent:#5aa9ff;--good:#2dd4a7;--warn:#f4b942;--bad:#e06c75;}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font:14px/1.6 -apple-system,"PingFang SC",
"Microsoft YaHei","Noto Sans CJK SC",sans-serif;padding:0 0 60px}
.wrap{max-width:1180px;margin:0 auto;padding:0 20px}
header{background:linear-gradient(135deg,#101b31,#0d1227);border-bottom:1px solid var(--line);padding:34px 0 26px;margin-bottom:26px}
h1{font-size:24px;font-weight:600;letter-spacing:.5px}
h1 small{color:var(--muted);font-size:13px;font-weight:400;margin-left:12px}
.meta{color:var(--muted);font-size:12px;margin-top:8px}
.meta b{color:var(--txt);font-weight:500}
h2{font-size:16px;font-weight:600;margin:34px 0 14px;color:#fff;
border-left:3px solid var(--accent);padding-left:10px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card .v{font-size:24px;font-weight:700;margin-top:2px}
.card .k{color:var(--muted);font-size:12px}
.badge{display:inline-block;font-size:12px;padding:2px 10px;border-radius:999px;font-weight:600}
.b-good{background:rgba(45,212,167,.14);color:var(--good)}
.b-warn{background:rgba(244,185,66,.14);color:var(--warn)}
.b-lock{background:rgba(130,150,179,.16);color:var(--muted)}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:14px;overflow-x:auto}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:860px){.grid2{grid-template-columns:1fr}}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:7px 10px;text-align:right;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-weight:500}
td:first-child,th:first-child{text-align:left}
.note{color:var(--muted);font-size:12px;margin-top:8px}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin:4px 2px 10px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px}
svg text{font-family:inherit}
footer{color:#5b6c85;font-size:12px;text-align:center;margin-top:44px;line-height:2}
a{color:var(--accent)}
</style>
</head>
<body>
<header><div class="wrap">
<h1>F01 风电功率预测 · 结果仪表盘<small>单机 10 分钟 SCADA · 首轮基线</small></h1>
<div class="meta" id="meta"></div>
</div></header>
<div class="wrap" id="app"></div>
<footer id="foot"></footer>
<script>
const D = /*__DATA__*/;
const $ = s => document.querySelector(s);
const el = (tag, cls, html) => {const e=document.createElement(tag); if(cls)e.className=cls; if(html!=null)e.innerHTML=html; return e;};
function svgLine(series, xLabels, w=520, h=240, opts={}) {
  // series: [{name,color,values:[..]}]
  const pad={l:52,r:12,t:12,b:30};
  const W=w-pad.l-pad.r, H=h-pad.t-pad.b;
  let mn=Infinity,mx=-Infinity;
  series.forEach(s=>s.values.forEach(v=>{if(isFinite(v)){mn=Math.min(mn,v);mx=Math.max(mx,v);}}));
  if(opts.zero){mn=Math.min(0,mn);} if(!isFinite(mn)){mn=0;mx=1;}
  if(mx===mn){mx=mn+1;}
  const X=i=>pad.l+W*i/Math.max(xLabels.length-1,1);
  const Y=v=>pad.t+H*(1-(v-mn)/(mx-mn));
  let g='';
  for(let i=0;i<=4;i++){const v=mn+(mx-mn)*i/4, y=Y(v);
    g+=`<line x1="${pad.l}" y1="${y}" x2="${w-pad.r}" y2="${y}" stroke="#263248" stroke-width="1"/>`;
    g+=`<text x="${pad.l-8}" y="${y+4}" fill="#8296b3" font-size="11" text-anchor="end">${v>=1000?(v/1000).toFixed(1)+'k':v.toFixed(v>=100?0:1)}</text>`;}
  const step=Math.ceil(xLabels.length/8);
  xLabels.forEach((t,i)=>{if(i%step===0)
    g+=`<text x="${X(i)}" y="${h-8}" fill="#8296b3" font-size="11" text-anchor="middle">${t}</text>`;});
  series.forEach(s=>{
    const pts=s.values.map((v,i)=>`${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(' ');
    g+=`<polyline points="${pts}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round"/>`;
  });
  if(opts.pts) opts.pts.forEach(p=>{
    g+=`<circle cx="${X(p.i)}" cy="${Y(p.v)}" r="3.5" fill="${p.color}"/>`;
  });
  return `<svg viewBox="0 0 ${w} ${h}" style="width:100%;height:auto">${g}</svg>`;
}
function svgBars(items, w=520, h=0, opts={}) {
  const rowH=opts.rowH||30, pad={l:150,r:60,t:4,b:4};
  const H=items.length*rowH+pad.t+pad.b, W=w;
  let mx=Math.max(...items.map(i=>i.v));
  let g='';
  items.forEach((it,i)=>{
    const y=pad.t+i*rowH+6, bw=Math.max(2,(W-pad.l-pad.r)*it.v/mx);
    g+=`<text x="${pad.l-10}" y="${y+13}" fill="#8296b3" font-size="12" text-anchor="end">${it.k}</text>`;
    g+=`<rect x="${pad.l}" y="${y}" width="${bw}" height="${rowH-12}" rx="4" fill="${it.color||'#5aa9ff'}"/>`;
    g+=`<text x="${pad.l+bw+8}" y="${y+13}" fill="#dbe4f0" font-size="12">${it.t}</text>`;
  });
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto">${g}</svg>`;
}
function svgBand(daily, keyHi, keyLo, keyMid, w=560, h=200) {
  const pad={l:52,r:12,t:10,b:26};
  const W=w-pad.l-pad.r, H=h-pad.t-pad.b;
  const vs=[]; daily.forEach(d=>{vs.push(d[keyHi],d[keyLo]);});
  const mn=Math.min(...vs), mx=Math.max(...vs);
  const X=i=>pad.l+W*i/Math.max(daily.length-1,1);
  const Y=v=>pad.t+H*(1-(v-mn)/(mx-mn||1));
  let g='';
  for(let i=0;i<=4;i++){const v=mn+(mx-mn)*i/4,y=Y(v);
    g+=`<line x1="${pad.l}" y1="${y}" x2="${w-pad.r}" y2="${y}" stroke="#263248"/>`;
    g+=`<text x="${pad.l-8}" y="${y+4}" fill="#8296b3" font-size="11" text-anchor="end">${(v/1000).toFixed(1)}k</text>`;}
  const ticks=[0,60,120,180,240,300,365].filter(t=>t<daily.length);
  ticks.forEach(t=>{g+=`<text x="${X(t)}" y="${h-6}" fill="#8296b3" font-size="11" text-anchor="middle">${daily[t]?daily[t].date.slice(5):''}</text>`;});
  let band='', mid='';
  daily.forEach((d,i)=>{const x=X(i),y1=Y(d[keyHi]),y2=Y(d[keyLo]);
    band+=`${x.toFixed(1)},${y1.toFixed(1)} `;});
  let band2=''; for(let i=daily.length-1;i>=0;i--){const x=X(i);band2+=`${x.toFixed(1)},${Y(daily[i][keyLo]).toFixed(1)} `;}
  g+=`<polygon points="${band}${band2}" fill="rgba(90,169,255,.16)" stroke="none"/>`;
  const pts=daily.map((d,i)=>`${X(i).toFixed(1)},${Y(d[keyMid]).toFixed(1)}`).join(' ');
  g+=`<polyline points="${pts}" fill="none" stroke="#5aa9ff" stroke-width="1.6"/>`;
  return `<svg viewBox="0 0 ${w} ${h}" style="width:100%;height:auto">${g}</svg>`;
}
function fmt(n,d=1){return (n==null?'—':Number(n).toLocaleString('zh-CN',{maximumFractionDigits:d}));}
function render(){
  const app=$('#app'), K=D.kpi;
  $('#meta').innerHTML=`数据集 <b>${D.dataset_sha}…</b> · ${D.time_range.start} ～ ${D.time_range.end} ·
    生成于 ${D.generated_at} · 代码 ${D.commit}`;
  // KPI
    const cards=[['有效记录',fmt(D.audit.time_axis.rows,0),'条 10 分钟 SCADA'],
    ['时间网格完整',K.grid_complete+'%','缺 99 个网格点'],
    ['样本覆盖率',K.coverage+'%','24h 历史窗口完备的发布'],
    ['负功率时段',K.neg_share+'%',`共 ${fmt(K.neg_count,0)} 条，原值保留不截零`],
    ['验证期最优 RMSE',fmt(K.val_rmse)+' kW',`模型 ${K.best_model}（验证期 ${D.splits.validation.issues} 个发布）`]];
    cards.push(D.test?['测试集最终 RMSE',fmt(K.test_rmse)+' kW',`模型 ${K.test_best} · 2024-11-01～12-12 一次性评分`]
                     :['验证门禁',K.gate.toUpperCase(),'测试集本轮锁定']);
  let c=el('section'); c.appendChild(el('div','cards'));
  c.firstChild.innerHTML=cards.map(x=>`<div class="card"><div class="k">${x[1]}</div>
    <div class="v" style="color:${x[0]==='验证门禁'&&K.gate==='pass'?'var(--good)':'var(--txt)'}">${x[0]}</div>
    <div class="k">${x[2]}</div></div>`).join('');
  app.appendChild(c);
  // 模型对比
  app.appendChild(el('h2',null,'验证期模型对比（2024-09-01 ～ 2024-11-01，同一 171,720 个有效样本）'));
  let p=el('div','grid2');
  const mm=Object.entries(D.models);
  p.innerHTML=`<div class="panel">${svgBars(mm.map(([n,v])=>({k:n,v:v.RMSE,t:v.RMSE.toFixed(1)+' kW',color:v.colors})))}
    <div class="note">RMSE（ pooled，kW）</div></div>
  <div class="panel">${svgBars(mm.map(([n,v])=>({k:n,v:v.MAE,t:v.MAE.toFixed(1)+' kW',color:v.colors})))}
    <div class="note">MAE（pooled，kW）</div></div>`;
  app.appendChild(p);
  let t=el('div','panel');
  t.innerHTML=`<table><tr><th>模型</th><th>MAE</th><th>RMSE</th><th>Bias</th><th>样本</th><th>单发布推理</th></tr>`+
    mm.map(([n,v])=>`<tr><td><i style="display:inline-block;width:9px;height:9px;border-radius:2px;background:${v.colors};margin-right:6px"></i>${n}</td>
    <td>${v.MAE.toFixed(1)}</td><td>${v.RMSE.toFixed(1)}</td><td>${v.Bias.toFixed(1)}</td><td>${fmt(v.n,0)}</td>
    <td>${v.runtime&&v.runtime.per_issue_ms!=null?v.runtime.per_issue_ms+' ms':'—'}</td></tr>`).join('')+`</table>
    <div class="note">标签为单机网侧有功功率 kW（含负值）；预测不裁剪。选择规则：Ridge alpha 与 LightGBM 早停仅用验证集。</div>`;
  app.appendChild(t);
  // per lead
  app.appendChild(el('h2',null,'分提前量误差（未来 10—240 分钟，每 10 分钟一值）'));
  const leads=[...new Set(D.leads.map(l=>l.lead))].sort((a,b)=>a-b);
  const mk=(field)=>Object.keys(D.models).map(m=>({name:m,color:D.models[m].color,
    values:leads.map(L=>{const r=D.leads.find(x=>x.model===m&&x.lead===L);return r?r[field]:null;})}));
  let pl=el('div','grid2');
  pl.innerHTML=`<div class="panel">${svgLine(mk('RMSE'),leads.map(String))}<div class="note">RMSE × 提前量：10—40 分钟持续法最优；约 50 分钟起学习模型反超。</div></div>
  <div class="panel">${svgLine(mk('MAE'),leads.map(String))}<div class="note">MAE × 提前量</div></div>`;
  app.appendChild(pl);
  // per day
  app.appendChild(el('h2',null,'逐日验证误差'));
  const days=[...new Set(D.days.map(d=>d.date))].sort();
  const dseries=Object.keys(D.models).map(m=>({name:m,color:D.models[m].color,
    values:days.map(dd=>{const r=D.days.find(x=>x.model===m&&x.date===dd);return r?r.RMSE:null;})}));
  let pd=el('div','panel');
  pd.innerHTML=svgLine(dseries,days.map(d=>d.slice(5)),560,260)+
    `<div class="note">每日 RMSE（kW）。${D.days.filter(x=>x.model==='ridge').reduce((s,x)=>s+x.n_issues,0)/days.length|0} 个发布/日均值。</div>`;
  app.appendChild(pd);
  // 测试集最终评分
  if(D.test){
    app.appendChild(el('h2',null,'测试集最终评分（2024-11-01 ～ 2024-12-12，冻结模型一次性评分）'));
    const tt=Object.entries(D.test.models);
    let tPanel=el('div','grid2');
    const tleads=[...new Set(D.test.leads.map(l=>l.lead))].sort((a,b)=>a-b);
    const tSeries=Object.keys(D.test.models).map(m=>({name:m,color:D.test.models[m].color,
      values:tleads.map(L=>{const r=D.test.leads.find(x=>x.model===m&&x.lead===L);return r?r.RMSE:null;})}));
    tPanel.innerHTML=`<div class="panel"><table>
      <tr><th>模型</th><th>MAE</th><th>RMSE</th><th>Bias</th><th>验证 RMSE</th></tr>`+
      tt.map(([n,v])=>`<tr><td><i style="display:inline-block;width:9px;height:9px;border-radius:2px;background:${v.colors};margin-right:6px"></i>${n}</td>
      <td>${v.MAE.toFixed(1)}</td><td><b>${v.RMSE.toFixed(1)}</b></td><td>${v.Bias.toFixed(1)}</td>
      <td style="color:#8296b3">${v.val_rmse.toFixed(1)}</td></tr>`).join('')+
      `</table><div class="note">测试期最优：${D.test.best}。验证期排序（Ridge 最优）未延续到测试期——
      一次性评分结果如实记录，此后不依据测试结果再做任何模型选择或调参。</div></div>
    <div class="panel">${svgLine(tSeries,tleads.map(String))}<div class="note">测试期 RMSE × 提前量</div></div>`;
    app.appendChild(tPanel);
  }
  // 分组诊断
  const B=D.best.groups;
  app.appendChild(el('h2',null,`分组与爬坡诊断（${D.best.model}）`));
  let gd=el('div','grid2');
  const bar=(rows,key,t)=>`<div class="panel">${svgBars(rows.map(r=>({k:r.group,v:r.MAE,t:r.MAE.toFixed(0)+' kW'})))}
    <div class="note">${t}</div></div>`;
  gd.innerHTML=bar(B.wind_groups,'MAE','风速分组（目标时刻风速，仅用于诊断分组，非模型输入）')+
    bar(B.power_groups.concat([]),'MAE','功率分组：negative <0 / zero =0 / low ≤5%训练最大 / high >90%训练最大');
  app.appendChild(gd);
  let gd2=el('div','grid2');
  gd2.innerHTML=bar(B.state_groups,'MAE','运行状态分组（发电/不发电/维护/环境异常，重叠统计）')+
    `<div class="panel"><table><tr><th>爬坡事件</th><th>阈值</th><th>事件内 MAE</th><th>方向准确率</th></tr>
    <tr><td>${D.ramp.events}</td><td>${D.ramp.threshold_kw} kW</td>
    <td>${(D.ramp.MAE_on_events||0).toFixed(0)}</td>
    <td>${((D.ramp.direction_accuracy||0)*100).toFixed(1)}%</td></tr></table>
    <div class="note">爬坡 = 目标前 30 分钟实际变化 |ΔP30| ≥ 训练期 95 分位（预注册阈值 ${D.thresholds.ramp_threshold_kw} kW）。</div></div>`;
  app.appendChild(gd2);
  // 时间序列
  app.appendChild(el('h2',null,'F01 日级功率包络（367 天，聚合值）'));
  let ts=el('div','panel');
  ts.innerHTML=svgBand(D.daily,'max','min','mean')+
    `<div class="note">每日最小—平均—最大网侧功率带（kW）。峰值约 5,314 kW。</div>`+
    svgLine([{name:'负功率占比',color:'#e06c75',values:D.daily.map(d=>d.neg_share)}],
      D.daily.map((_,i)=>i%90===0?D.daily[i].date.slice(5):''),560,150)+
    `<div class="note">每日负功率样本占比（%）：停机辅助用电，原值保留。</div>`;
  app.appendChild(ts);
  // 审计
  app.appendChild(el('h2',null,'数据质量审计（规则只标记、不删改）'));
  const flags=Object.entries(D.audit.row_flags).filter(([k])=>!k.startsWith('parse'));
  let qa=el('div','grid2');
  qa.innerHTML=`<div class="panel"><table><tr><th>质量标记</th><th>命中行数</th></tr>`+
    flags.map(([k,v])=>`<tr><td>${k}</td><td>${fmt(v,0)}</td></tr>`).join('')+`</table>
    <div class="note">${D.audit.rules_note}</div></div>
    <div class="panel"><table>
    <tr><th>时间轴</th><th>值</th></tr>
    <tr><td>缺口事件</td><td>${D.audit.time_axis.gap_events} 次</td></tr>
    <tr><td>最大缺口</td><td>${D.audit.time_axis.max_gap_minutes} 分钟（2024-05-27）</td></tr>
    <tr><td>风速范围</td><td>0 ～ ${D.audit.wind.max_ms} m/s（中位 ${D.audit.wind.median_ms}）</td></tr>
    <tr><td>设备 / 型号</td><td>${D.audit.identity.设备名称} / ${D.audit.identity.型号名称}</td></tr>
    <tr><td>额定功率</td><td>${K.rated_note}</td></tr></table></div>`;
  app.appendChild(qa);
  $('#foot').innerHTML=`快照 ${D.best.name} · 训练 ${D.splits.train.issues} 发布 / 验证 ${D.splits.validation.issues} 发布 / 测试 ${D.splits.test.issues} 发布（${D.test?'已一次性评分':'本轮锁定'}）`;
}
render();
</script>
</body>
</html>
"""


def main() -> None:
    payload = build_payload()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    html = HTML.replace('/*__DATA__*/', json.dumps(payload, ensure_ascii=False))
    OUT.write_text(html, encoding='utf-8')
    print(f'wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KB)')


if __name__ == '__main__':
    main()

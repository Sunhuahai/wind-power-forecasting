"""Evidence-backed progress chart; retain successful RMSE milestones only."""
import argparse
import csv
from datetime import date
import json
import math
import re
import textwrap
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import MultipleLocator, FormatStrFormatter

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "docs/optimization-progress.json"
OUT = ROOT / "docs/figures/optimization-progress"


def score(event):
    path = ROOT / event["source"]
    if path.suffix == ".csv":
        with path.open() as file:
            rows = [r for r in csv.DictReader(file) if r["model"] == event["model"]]
        if len(rows) != 1:
            raise ValueError(f"Expected one result for {event['id']}")
        row = rows[0]
    else:
        row = json.loads(path.read_text())
        for key in event.get("json_key", "pooled").split("."):
            row = row[key]
    value = dict(RMSE=float(row["RMSE"]), MAE=float(row["MAE"]), n=int(row["n"]))
    if not all(math.isfinite(value[k]) and value[k] >= 0 for k in ["RMSE", "MAE"]):
        raise ValueError("Invalid metrics")
    return value


def resolve(registry):
    rows = []
    for event in registry["milestones"]:
        row = dict(**event, **score(event))
        if rows and (row["n"] != rows[0]["n"] or row["RMSE"] >= rows[-1]["RMSE"]):
            raise ValueError("Progress must improve RMSE on the same sample count")
        if rows and row["date"] < rows[-1]["date"]:
            raise ValueError("Milestones must be chronological")
        if "paired" in event:
            paired = json.loads((ROOT / event["paired"]).read_text())
            if paired["delta_RMSE_high"] >= 0:
                raise ValueError("Paired evidence does not establish RMSE improvement")
            if rows and not math.isclose(paired["delta_RMSE"], row["RMSE"] - rows[-1]["RMSE"], abs_tol=1e-12):
                raise ValueError("Paired comparison does not match preceding milestone")
        rows.append(row)
    return rows


def render(rows, cohort):
    OUT.parent.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"font.family": "sans-serif", "font.sans-serif": ["DejaVu Sans"], "font.size": 10,
                         "pdf.fonttype": 42, "svg.fonttype": "none",
                         "axes.spines.top": False, "axes.spines.right": False})
    fig, ax = plt.subplots(figsize=(min(12,max(10,1.9*len(rows))), 5.8), facecolor="white")
    fig.subplots_adjust(left=.12, right=.94, bottom=.32, top=.75)
    x = list(range(len(rows)))
    y = [r["RMSE"] for r in rows]
    gain = (1 - y[-1] / y[0]) * 100
    fig.text(.12, .92, "Wind power forecasting", size=20, weight="bold", color="#182b3a")
    fig.text(.12, .865, "Optimization progress  /  successful milestones only", size=11, color="#5b6874")
    fig.text(.94, .92, f"{y[-1]:.6f}", ha="right", size=24, weight="bold", color="#087f6f")
    fig.text(.94, .865, f"Best RMSE  |  {gain:.2f}% lower than baseline", ha="right", size=10, color="#087f6f")
    ax.step(x, y, where="post", color="#087f6f", lw=2.4, zorder=2)
    ax.scatter(x, y, color="#087f6f", s=68, edgecolors="white", linewidths=1.4, zorder=3)
    labels=[]
    for i,r in enumerate(rows):
        lines=textwrap.wrap(f"{i+1:02d}  {r['label']}",width=16,break_long_words=False,break_on_hyphens=False)
        method='\n'.join(lines+['']*(2-len(lines)))
        labels.append(f"{method}\n{r['date']}\n{r['RMSE']:.6f}")
    ax.set_xticks(x, labels)
    ax.tick_params(axis="x", length=0, pad=15, labelsize=9 if len(rows)>=9 else 10)
    span = max(y) - min(y)
    ax.set_ylim(min(y)-max(span*.25, .001), max(y)+max(span*.32, .001))
    ax.set_xlim(-.22, len(rows)-.78)
    ax.set_ylabel("Test RMSE  (lower is better)", labelpad=12)
    ax.yaxis.set_major_locator(MultipleLocator(.002 if span < .015 else .005))
    ax.yaxis.set_major_formatter(FormatStrFormatter("%.3f"))
    ax.grid(axis="y", color="#e5e9ec", lw=.8)
    ax.set_axisbelow(True)
    ax.spines[["left", "bottom"]].set_color("#bac4cb")
    notes=[r['protocol_note'] for r in rows if r.get('protocol_note')]
    fig.text(.12, .105 if notes else .09, f"Same historical test: 10 zones / {rows[0]['n']:,} hours / Jul-Dec 2013", size=9, color="#5b6874")
    fig.text(.12, .065 if notes else .045, "Exploratory benchmark. Steps show completed milestones, not elapsed time or learning curves.", size=8.5, color="#5b6874")
    if notes:
        fig.text(.12,.025,'* '+notes[-1],size=8.5,color='#795a20')
    fig.savefig(f"{OUT}.png", dpi=300, facecolor="white")
    fig.savefig(f"{OUT}.pdf", facecolor="white")
    fig.savefig(f"{OUT}.svg", facecolor="white")
    plt.close(fig)
    fields = ["step", "date", "method", "MAE", "RMSE", "n", "gain_vs_baseline_pct", "source"]
    with OUT.with_suffix(".csv").open("w") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for i, r in enumerate(rows):
            writer.writerow(dict(step=i+1, date=r["date"], method=r["label_zh"], MAE=r["MAE"], RMSE=r["RMSE"],
                                 n=r["n"], gain_vs_baseline_pct=(1-r["RMSE"]/y[0])*100, source=r["source"]))
    return gain


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--add-result", help="Result CSV or summary JSON, relative to repo")
    parser.add_argument("--id")
    parser.add_argument("--label")
    parser.add_argument("--label-zh")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--model")
    parser.add_argument("--json-key", default="pooled")
    parser.add_argument("--paired")
    parser.add_argument("--protocol-note", help="Visible note for a training-protocol change")
    args = parser.parse_args()
    registry = json.loads(REGISTRY.read_text())
    rows = resolve(registry)
    if args.add_result:
        if not args.id or not args.label or not args.label_zh:
            parser.error("--id, --label and --label-zh are required with --add-result")
        event = dict(id=args.id, date=args.date, label=args.label, label_zh=args.label_zh,
                     source=args.add_result, json_key=args.json_key)
        if args.model:
            event["model"] = args.model
        if args.paired:
            event["paired"] = args.paired
        if args.protocol_note:
            event['protocol_note']=args.protocol_note
        if any(r["id"] == args.id for r in rows):
            parser.error("Milestone ID already exists")
        if score(event)["RMSE"] >= rows[-1]["RMSE"]:
            print("No improvement; candidate discarded from progress.")
            return
        registry["milestones"].append(event)
        rows = resolve(registry)
        REGISTRY.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n")
    gain = render(rows, registry["cohort"])
    note = ROOT / 'docs/optimization-progress.md'
    table = ['|进展|日期|十站测试 RMSE|相对前一节点降低|', '|---|---|---:|---:|']
    for i, row in enumerate(rows):
        reduction = '—' if i == 0 else f"{(1-row['RMSE']/rows[i-1]['RMSE'])*100:.4f}%"
        table.append(f"|{row['label_zh']}|{row['date']}|{row['RMSE']:.9f}|{reduction}|")
    table += ['', f"累计 RMSE 降低 **{gain:.2f}%**。各节点均为同一十站、{rows[0]['n']:,} 小时、2013 年下半年历史测试集。"]
    text = note.read_text()
    text, count = re.subn(r'<!-- progress-table:start -->.*?<!-- progress-table:end -->',
                         '<!-- progress-table:start -->\n'+'\n'.join(table)+'\n<!-- progress-table:end -->',
                         text, flags=re.S)
    if count != 1:
        raise ValueError('Progress table markers missing or duplicated')
    note.write_text(text)
    print(f"{len(rows)} successful milestones; best RMSE {rows[-1]['RMSE']:.9f}; gain {gain:.3f}%")


if __name__ == "__main__":
    main()

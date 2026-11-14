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
from matplotlib.font_manager import FontProperties
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


SHORT = {
    "kpower": ("邻域 + 跨站 OOF", "Neighborhood + OOF"),
    "validation_blend": ("验证集凸融合", "Validation blend"),
    "spatial_nwp_blend": ("十站原始风场", "Spatial wind field"),
    "temporal_nwp_blend": ("风速时变", "Wind dynamics"),
    "shared_model_blend": ("跨站共享模型", "Shared model"),
    "curve_residual_blend": ("场站曲线残差", "Curve residual"),
    "mlp_shared_blend": ("标准化 MLP", "Standardized MLP"),
    "shared_final_refit": ("共享重拟合*", "Shared refit*"),
    "mlp_final_refit": ("MLP 重拟合*", "MLP refit*"),
    "temporal_final_refit": ("时变重拟合*", "Temporal refit*"),
}

# Candidates that were evaluated on the same frozen test set and rejected.
# Only candidates with a recorded test score appear; validation-gated ones do not.
CANDIDATES = [
    "site_blend_v1", "diversity_v1", "expanded_refit_v1", "cross_history_v1",
    "vertical_shared_v1", "day_context_shared_v1", "extra_trees_v1", "joint_blend_v1",
    "prediction_calibration_v1", "hist_gradient_v1", "kernel_ridge_v1",
    "mlp_seed_ensemble_v1", "shared_median_v1", "direct_full_year_v1",
    "shared_capacity_search_v1", "forecast_bias_shared_v1", "direction_shared_v1",
    "lead_blend_v1",
]
ABLATION = ROOT / "artifacts/kpower_ablation/ablation_20261114_v1"

CJK_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)
GREEN = "#2f9e62"


def cjk_font():
    for path in CJK_CANDIDATES:
        if Path(path).exists():
            try:
                return FontProperties(fname=path)
            except Exception:
                continue
    return None


def candidate_points(rows):
    """Rejected candidates, placed just after the milestone they were tested against."""
    out = []
    for name in CANDIDATES:
        path = ABLATION / name / "metrics.csv"
        if not path.exists():
            continue
        with path.open() as file:
            records = list(csv.DictReader(file))
        if len(records) < 2:
            continue
        base, cand = records[0], records[-1]
        try:
            base_rmse, cand_rmse = float(base["RMSE"]), float(cand["RMSE"])
        except (KeyError, ValueError):
            continue
        slot = next((i for i, r in enumerate(rows) if abs(r["RMSE"] - base_rmse) < 1e-9), None)
        if slot is None:
            continue
        out.append(dict(path=name, slot=slot, rmse=cand_rmse, model=cand["model"]))
    buckets = {}
    for item in out:
        buckets.setdefault(item["slot"], []).append(item)
    placed = []
    for slot, items in buckets.items():
        for k, item in enumerate(sorted(items, key=lambda r: r["path"])):
            placed.append(dict(item, x=slot + 0.16 + 0.11 * k))
    return placed


def render(rows, cohort):
    fp = cjk_font()
    zh = fp is not None
    if not zh:
        fp = FontProperties(family="DejaVu Sans")
    plt.rcParams.update({"font.family": "sans-serif", "font.sans-serif": ["DejaVu Sans"], "font.size": 10,
                         "pdf.fonttype": 42, "svg.fonttype": "none",
                         "axes.spines.top": False, "axes.spines.right": False})
    fig, ax = plt.subplots(figsize=(13, 6.2), facecolor="white")
    fig.subplots_adjust(left=.068, right=.985, bottom=.115, top=.885)
    x = list(range(len(rows)))
    y = [r["RMSE"] for r in rows]
    gain = (1 - y[-1] / y[0]) * 100
    kept_label = "成功节点" if zh else "Kept"
    cand_label = "候选（未采纳）" if zh else "Discarded"
    line_label = "最优序列" if zh else "Running best"

    cands = candidate_points(rows)
    if cands:
        ax.scatter([c["x"] for c in cands], [c["rmse"] for c in cands], s=26,
                   color="#c2c8cc", edgecolors="none", zorder=2, label=cand_label)
    ax.step(x, y, where="post", color=GREEN, lw=2.1, zorder=3, label=line_label)
    ax.scatter(x, y, s=58, facecolors="white", edgecolors=GREEN, linewidths=1.7,
               zorder=4, label=kept_label)

    for i, row in enumerate(rows):
        if i == 0:
            continue
        bare = (1 - row["RMSE"] / rows[i - 1]["RMSE"]) * 100
        label = SHORT.get(row["id"])
        if not label:
            label = (row["label_zh"], row["label"])
        text = f"{label[0] if zh else label[1]}  −{bare:.2f}%"
        last = i == len(rows) - 1
        ax.annotate(text, xy=(x[i], y[i]), xytext=(-26 if last else 6, 12),
                    textcoords="offset points", ha="left", va="bottom",
                    rotation=26, rotation_mode="anchor", fontsize=8.2, color="#333f47",
                    fontproperties=fp, zorder=5)
    ax.annotate("原 C 基线" if zh else "baseline", xy=(0, y[0]), xytext=(8, 10),
                textcoords="offset points", ha="left", va="bottom", fontsize=8.4,
                color="#7d8892", fontproperties=fp)
    if zh:
        subtitle = ("风电功率预测优化进展："
                    f"{len(rows) - 1} 次改进、{len(rows)} 个成功节点" + (f"，{len(cands)} 个候选被否决" if cands else ""))
        best = f"最优 RMSE {y[-1]:.6f}  |  较原 C 基线低 {gain:.2f}%"
        xlabel = "优化轮次"
        ylabel = "测试 RMSE（越低越好）"
        foot = (f"同一历史测试集：十站 / {rows[0]['n']:,} 小时 / 2013 年 7–12 月。"
                "严格协议：只用 2012 年训练、验证集只做选择与早停。灰点为同测试集上被否决的候选。")
        if any(r.get("protocol_note") for r in rows):
            foot += " * 为最终重拟合（冻结原选轮数，把 2013 上半年标签并入训练，协议变化）。"
    else:
        subtitle = (f"Wind power optimization: {len(rows) - 1} improvements, {len(rows)} kept nodes")
        best = f"Best RMSE {y[-1]:.6f}  |  {gain:.2f}% below the original C baseline"
        xlabel, ylabel = "Optimization round", "Test RMSE  (lower is better)"
        foot = (f"Same historical test: 10 zones / {rows[0]['n']:,} hours / Jul-Dec 2013. "
                "Strict protocol: 2012-only training, validation for selection only. Grey dots: rejected candidates.")
        if any(r.get("protocol_note") for r in rows):
            foot += " * final refit (frozen rounds, Jan-Jun 2013 labels)."
    ax.set_title(subtitle, fontsize=14, color="#1b2733", pad=10, loc="left", fontproperties=fp)
    ax.text(1.0, 1.045, best, transform=ax.transAxes, ha="right", va="bottom",
            fontsize=10, color=GREEN, fontproperties=fp)
    ax.set_xticks(x, (["0"] + [str(i) for i in range(1, len(rows))]))
    ax.tick_params(axis="x", length=0, pad=6, labelsize=9.5)
    ax.set_xlabel(xlabel, fontproperties=fp, color="#5b6874")
    ax.set_ylabel(ylabel, fontproperties=fp)
    span = max(y) - min(y)
    ax.set_ylim(min(y) - span * .06, max(y) + span * .10)
    ax.set_xlim(-.45, len(rows) - .35)
    ax.yaxis.set_major_locator(MultipleLocator(.002 if span < .015 else .005))
    ax.yaxis.set_major_formatter(FormatStrFormatter("%.3f"))
    ax.grid(axis="y", color="#eceff1", lw=.8)
    ax.set_axisbelow(True)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#c8d0d6")
    leg = ax.legend(loc="upper right", bbox_to_anchor=(0.995, 0.94), frameon=False,
                    fontsize=9, prop=fp, handletextpad=.6, borderaxespad=0)
    fig.text(.068, .022, foot, size=8, color="#6b7680", fontproperties=fp)
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

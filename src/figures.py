"""Make the abstract's table and figure from results/metrics.csv.

Outputs:
  results/table.tex, results/table.md   judges x (balanced acc., kappa, FPR) for both conditions
  figures/fpr_by_length.{png,pdf}       FPR by trajectory-length tertile, one line per judge,
                                        panels: final patch only | full trajectory

Usage: python src/figures.py
"""

import os
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_data  # noqa: E402

ROOT = fetch_data.ROOT

# Categorical slots 1-4 of the reference palette (validated: CVD ΔE >= 9.1). Slots 3-4 are
# below 3:1 contrast on white, so every series also gets its own marker (secondary encoding).
COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
MARKERS = ["o", "s", "^", "D"]
INK, MUTED, GRID = "#1f1f1e", "#6b6a64", "#e4e3de"

LABELS = {"deepseek-v3.2": "DeepSeek-V3.2", "gemma-4-31b": "Gemma 4 31B",
          "nemotron-3-ultra": "Nemotron 3 Ultra", "gpt-5-mini": "GPT-5-mini"}
COND_LABELS = {"final_only": "Final patch only", "full": "Full trajectory"}


def get(m, section, judge, cond, metric, group="all"):
    r = m[(m.section == section) & (m.judge == judge) & (m.condition == cond)
          & (m.group == group) & (m.metric == metric)]
    return r.iloc[0] if len(r) else None


def write_table(m, judges):
    conds = ["final_only", "full"]
    metrics = [("balanced_accuracy", "BA"), ("kappa", r"$\kappa$"), ("fpr", "FPR")]

    def cell(r):
        return f"{r.value:.2f} [{r.ci_lo:.2f}, {r.ci_hi:.2f}]" if r is not None else "--"

    def cell_tex(r):
        return (f"{r.value:.2f} {{\\scriptsize[{r.ci_lo:.2f}, {r.ci_hi:.2f}]}}" if r is not None else "--")

    tex = [r"\begin{tabular}{l" + "c" * 6 + "}", r"\toprule",
           r" & \multicolumn{3}{c}{Final patch only} & \multicolumn{3}{c}{Full trajectory} \\",
           r"\cmidrule(lr){2-4}\cmidrule(lr){5-7}",
           "Judge & " + " & ".join(lbl for _ in conds for _, lbl in metrics) + r" \\", r"\midrule"]
    md = ["| Judge | " + " | ".join(f"{COND_LABELS[c]}: {lbl.replace('$\\kappa$', 'kappa')}"
                                      for c in conds for _, lbl in metrics) + " |",
          "|---|" + "---|" * 6]
    for jn in judges:
        rows = [get(m, "overall", jn, c, k) for c in conds for k, _ in metrics]
        tex.append(LABELS.get(jn, jn) + " & " + " & ".join(cell_tex(r) for r in rows) + r" \\")
        md.append(f"| {LABELS.get(jn, jn)} | " + " | ".join(cell(r) for r in rows) + " |")
    tex += [r"\bottomrule", r"\end{tabular}"]
    with open(os.path.join(ROOT, "results", "table.tex"), "w", encoding="utf-8") as f:
        f.write("\n".join(tex) + "\n")
    with open(os.path.join(ROOT, "results", "table.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")


def write_figure(m, judges, cuts):
    plt.rcParams.update({
        "font.family": "sans-serif", "font.size": 7, "axes.titlesize": 7.5, "axes.labelsize": 7,
        "xtick.labelsize": 6.5, "ytick.labelsize": 6.5, "legend.fontsize": 6.5,
        "axes.edgecolor": MUTED, "axes.labelcolor": INK, "xtick.color": MUTED, "ytick.color": MUTED,
        "text.color": INK, "pdf.fonttype": 42,
    })
    fig, axes = plt.subplots(1, 2, figsize=(3.4, 2.15), sharey=True)
    x = [0, 1, 2]
    dodge = [-0.09, -0.03, 0.03, 0.09]
    for ax, cond in zip(axes, ["final_only", "full"]):
        for k, jn in enumerate(judges):
            rs = [get(m, "length_tertile", jn, cond, "fpr", f"T{t}") for t in (1, 2, 3)]
            if any(r is None for r in rs):
                continue
            xs = [xi + dodge[k] for xi in x]
            ys = [r.value for r in rs]
            err = [[r.value - r.ci_lo for r in rs], [r.ci_hi - r.value for r in rs]]
            ax.errorbar(xs, ys, yerr=err, fmt="none", ecolor=COLORS[k], elinewidth=0.6, alpha=0.45, capsize=0)
            ax.plot(xs, ys, color=COLORS[k], lw=1.5, marker=MARKERS[k], ms=3.6,
                    markeredgecolor="white", markeredgewidth=0.6, label=LABELS.get(jn, jn), zorder=3)
        ax.set_title(COND_LABELS[cond], color=INK, pad=3)
        ax.set_xticks(x)
        ax.set_xticklabels(["Short", "Medium", "Long"])
        ax.set_xlim(-0.35, 2.35)
        ax.set_ylim(0, 1.04)
        ax.grid(axis="y", color=GRID, lw=0.6)
        ax.set_axisbelow(True)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        ax.tick_params(length=2, width=0.6)
    axes[0].set_ylabel("False positive rate\n(failed runs judged successful)")
    fig.supxlabel(f"Trajectory length tertile (cuts at {cuts[0] / 1000:.0f}k / {cuts[1] / 1000:.0f}k tokens)",
                  fontsize=6.5, color=MUTED, y=0.075)
    handles, labels = axes[1].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=2, frameon=False, bbox_to_anchor=(0.55, 1.02),
               handlelength=1.6, columnspacing=1.0)
    fig.tight_layout(rect=(0, 0.06, 1, 0.86), w_pad=0.6)
    os.makedirs(os.path.join(ROOT, "figures"), exist_ok=True)
    for ext in ("png", "pdf"):
        fig.savefig(os.path.join(ROOT, "figures", f"fpr_by_length.{ext}"), dpi=300, bbox_inches="tight")


def main():
    cfg = fetch_data.load_config()
    judges = [j["name"] for j in cfg["judge"]["judges"]]
    m = pd.read_csv(os.path.join(ROOT, "results", "metrics.csv"))
    preds = pd.read_csv(os.path.join(ROOT, "results", "predictions.csv"))
    toks = preds.drop_duplicates("item_id")["tokens"]
    cuts = toks.quantile([1 / 3, 2 / 3]).values
    write_table(m, judges)
    write_figure(m, judges, cuts)
    print("wrote results/table.tex, results/table.md, figures/fpr_by_length.png/.pdf")


if __name__ == "__main__":
    main()

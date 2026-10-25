"""Compute judge-reliability metrics from the judge cache.

Inputs:  data/sample.jsonl, cache/<judge>/<condition>/<item_id>.json, config.yaml
Outputs: results/predictions.csv   one row per (judge, condition, item)
         results/metrics.csv       long format: section, judge, condition, group, metric, value, ci_lo, ci_hi, n
         results/summary.md        readable summary
         results/error_candidates.md  high-confidence false positives with rationales

Confidence intervals: 95% percentile bootstrap, 1,000 resamples of *pairs* (cluster bootstrap),
because the two trajectories in a pair share an instance and are not independent.
Missing calls and parse failures are excluded from metrics and reported as coverage.

Usage: python src/metrics.py [--n-boot 1000] [--seed 20261015]
"""

import argparse
import datetime as dt
import json
import os
import sys

import krippendorff
import numpy as np
import pandas as pd
import statsmodels.formula.api as smf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_data  # noqa: E402
import judge as judge_mod  # noqa: E402

ROOT = fetch_data.ROOT


# ---------------------------------------------------------------- data

def load_predictions(cfg):
    with open(os.path.join(ROOT, "data", "sample.jsonl"), encoding="utf-8") as f:
        items = [json.loads(line) for line in f]
    tok = np.array([x["length"]["tokens"] for x in items])
    cuts = np.quantile(tok, [1 / 3, 2 / 3])
    rows = []
    for j in cfg["judge"]["judges"]:
        for cond in cfg["judge"]["conditions"]:
            for x in items:
                path = judge_mod.cache_path(j["name"], cond, x["item_id"])
                rec = None
                if os.path.exists(path):
                    with open(path, encoding="utf-8") as f:
                        rec = json.load(f)
                res = rec["result"] if rec and rec["parse_ok"] else None
                t = x["length"]["tokens"]
                rows.append({
                    "judge": j["name"], "judge_family": j["family"], "condition": cond,
                    "item_id": x["item_id"], "pair_id": x["pair_id"], "instance_id": x["instance_id"],
                    "agent_family": x["agent_family"], "resolved": int(x["resolved"]),
                    "tokens": t, "n_turns": x["length"]["n_turns"],
                    "length_tertile": int(np.searchsorted(cuts, t, side="right")),
                    "status": "ok" if res else ("parse_fail" if rec else "missing"),
                    "pred": (1 if res["verdict"] == "success" else 0) if res else np.nan,
                    "confidence": res["confidence"] if res else np.nan,
                    "rationale": res["rationale"] if res else "",
                    "prompt_version": rec["prompt_version"] if rec else "",
                    "provider": rec["attempts"][-1].get("provider") if rec else "",
                })
    df = pd.DataFrame(rows)
    df["correct"] = (df["pred"] == df["resolved"]).astype(float).where(df["pred"].notna())
    df["same_family"] = (df["judge_family"] == df["agent_family"]).astype(int)
    return df, cuts


# ---------------------------------------------------------------- metrics

def point_metrics(y, p):
    """y, p: 0/1 arrays without NaNs."""
    out = {}
    pos, neg = y == 1, y == 0
    tpr = p[pos].mean() if pos.any() else np.nan
    fpr = p[neg].mean() if neg.any() else np.nan
    out["accuracy"] = (y == p).mean() if len(y) else np.nan
    out["balanced_accuracy"] = (tpr + (1 - fpr)) / 2
    out["tpr"] = tpr
    out["fpr"] = fpr
    out["fnr"] = 1 - tpr
    out["success_rate"] = p.mean() if len(p) else np.nan
    po = out["accuracy"]
    pe = y.mean() * p.mean() + (1 - y.mean()) * (1 - p.mean())
    out["kappa"] = (po - pe) / (1 - pe) if pe < 1 else np.nan
    return out


class PairBootstrap:
    """Resample pairs with replacement; returns item index arrays per replicate."""

    def __init__(self, pair_ids, n_boot, seed):
        codes, uniq = pd.factorize(pd.Series(pair_ids))
        self.members = [np.flatnonzero(codes == k) for k in range(len(uniq))]
        rng = np.random.default_rng(seed)
        self.draws = rng.integers(0, len(uniq), size=(n_boot, len(uniq)))

    def indices(self):
        """Yields (row indices, draw slot per row). The slot makes repeated pairs distinct."""
        for d in self.draws:
            idx = [self.members[k] for k in d]
            yield np.concatenate(idx), np.repeat(np.arange(len(d)), [len(i) for i in idx])


def with_ci(fn, sub, boot_seed, n_boot):
    """fn(sub_df) -> dict of metrics; adds bootstrap CIs over pairs.

    A pair drawn twice must count twice, so ids are made unique per draw slot; otherwise
    metrics that pivot or merge on pair_id/item_id would silently collapse the duplicates.
    """
    point = fn(sub)
    bs = PairBootstrap(sub["pair_id"].values, n_boot, boot_seed)
    reps = {k: [] for k in point}
    for idx, slot in bs.indices():
        s = sub.iloc[idx].copy()
        suffix = "#" + pd.Series(slot, index=s.index).astype(str)
        s["pair_id"] = s["pair_id"] + suffix
        s["item_id"] = s["item_id"] + suffix
        m = fn(s)
        for k in point:
            reps[k].append(m[k])
    out = {}
    for k, v in point.items():
        arr = np.array(reps[k], dtype=float)
        arr = arr[~np.isnan(arr)]
        lo, hi = (np.percentile(arr, [2.5, 97.5]) if len(arr) else (np.nan, np.nan))
        out[k] = (v, lo, hi)
    return out


def judge_metrics(sub):
    ok = sub[sub["pred"].notna()]
    return point_metrics(ok["resolved"].values, ok["pred"].values.astype(int))


def pair_metrics(sub):
    """Fraction of pairs the judge separates correctly (R=success, U=failure) or reverses."""
    ok = sub[sub["pred"].notna()]
    w = ok.pivot_table(index="pair_id", columns="resolved", values="pred", aggfunc="first").dropna()
    if not len(w):
        return {"pair_separated": np.nan, "pair_reversed": np.nan}
    return {"pair_separated": ((w[1] == 1) & (w[0] == 0)).mean(),
            "pair_reversed": ((w[1] == 0) & (w[0] == 1)).mean()}


def alpha_nominal(sub, judges):
    w = sub.pivot_table(index="item_id", columns="judge", values="pred", aggfunc="first").reindex(columns=judges)
    data = w.T.values.astype(float)
    if np.isfinite(data).sum() < 2:
        return np.nan
    return krippendorff.alpha(reliability_data=data, level_of_measurement="nominal")


def condition_delta(sub_full, sub_fo):
    """Paired difference full - final_only on the same items."""
    m = sub_full.merge(sub_fo, on=["item_id", "pair_id", "resolved"], suffixes=("_f", "_o"))
    m = m[m["pred_f"].notna() & m["pred_o"].notna()]
    neg = m[m["resolved"] == 0]
    return {
        "delta_success_rate": m["pred_f"].mean() - m["pred_o"].mean(),
        "delta_fpr": neg["pred_f"].mean() - neg["pred_o"].mean(),
        "delta_balanced_accuracy": (point_metrics(m["resolved"].values, m["pred_f"].values.astype(int))["balanced_accuracy"]
                                    - point_metrics(m["resolved"].values, m["pred_o"].values.astype(int))["balanced_accuracy"]),
    }


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--n-boot", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=None, help="default: sample.seed from config.yaml")
    args = ap.parse_args()
    cfg = fetch_data.load_config()
    seed = args.seed if args.seed is not None else cfg["sample"]["seed"]
    judges = [j["name"] for j in cfg["judge"]["judges"]]
    conds = cfg["judge"]["conditions"]
    os.makedirs(os.path.join(ROOT, "results"), exist_ok=True)

    df, cuts = load_predictions(cfg)
    df.to_csv(os.path.join(ROOT, "results", "predictions.csv"), index=False)

    rows = []

    def add(section, judge, cond, group, res, n):
        for k, (v, lo, hi) in res.items():
            rows.append({"section": section, "judge": judge, "condition": cond, "group": group,
                         "metric": k, "value": v, "ci_lo": lo, "ci_hi": hi, "n": n})

    for jn in judges:
        for cond in conds:
            sub = df[(df.judge == jn) & (df.condition == cond)]
            n_ok = int(sub["pred"].notna().sum())
            rows.append({"section": "coverage", "judge": jn, "condition": cond, "group": "all", "metric": "n_ok",
                         "value": n_ok, "ci_lo": np.nan, "ci_hi": np.nan, "n": len(sub)})
            for st in ("parse_fail", "missing"):
                rows.append({"section": "coverage", "judge": jn, "condition": cond, "group": "all", "metric": st,
                             "value": int((sub["status"] == st).sum()), "ci_lo": np.nan, "ci_hi": np.nan, "n": len(sub)})
            add("overall", jn, cond, "all", with_ci(judge_metrics, sub, seed, args.n_boot), n_ok)
            add("pairs", jn, cond, "all", with_ci(pair_metrics, sub, seed, args.n_boot), n_ok)
            ok = sub[sub.pred.notna()]
            add("overall", jn, cond, "all",
                {"mean_confidence": (ok.confidence.mean(), np.nan, np.nan),
                 "mean_confidence_when_wrong": (ok[ok.correct == 0].confidence.mean(), np.nan, np.nan)}, n_ok)
            for t in range(3):
                s = sub[sub.length_tertile == t]
                add("length_tertile", jn, cond, f"T{t + 1}", with_ci(judge_metrics, s, seed, args.n_boot),
                    int(s.pred.notna().sum()))
            for fam in sorted(df.agent_family.unique()):
                s = sub[sub.agent_family == fam]
                add("agent_family", jn, cond, fam, with_ci(judge_metrics, s, seed, args.n_boot),
                    int(s.pred.notna().sum()))
            # RQ4: same-family agent vs other agents (only for judges whose family has an agent)
            if sub.same_family.any():
                for flag, name in ((1, "same_family"), (0, "other_family")):
                    s = sub[sub.same_family == flag]
                    add("self_preference", jn, cond, name, with_ci(judge_metrics, s, seed, args.n_boot),
                        int(s.pred.notna().sum()))

        # RQ2: full vs final_only, paired on items
        f = df[(df.judge == jn) & (df.condition == "full")][["item_id", "pair_id", "resolved", "pred"]]
        o = df[(df.judge == jn) & (df.condition == "final_only")][["item_id", "pair_id", "resolved", "pred"]]
        both = f.merge(o, on=["item_id", "pair_id", "resolved"], suffixes=("_f", "_o"))

        def delta_fn(b):
            return condition_delta(b.rename(columns={"pred_f": "pred"})[["item_id", "pair_id", "resolved", "pred"]],
                                   b.rename(columns={"pred_o": "pred"})[["item_id", "pair_id", "resolved", "pred"]])
        add("condition_delta", jn, "full-final_only", "all", with_ci(delta_fn, both, seed, args.n_boot),
            int((both.pred_f.notna() & both.pred_o.notna()).sum()))

    # Inter-judge agreement per condition
    for cond in conds:
        sub = df[df.condition == cond]
        add("agreement", "all_judges", cond, "all",
            with_ci(lambda s: {"krippendorff_alpha": alpha_nominal(s, judges)}, sub, seed, args.n_boot),
            int(sub.item_id.nunique()))

    # Length effect controlling for agent family (logistic regression, pair-clustered SEs)
    for jn in judges:
        for cond in conds:
            s = df[(df.judge == jn) & (df.condition == cond) & df.pred.notna()].copy()
            s["log2_tokens_z"] = (np.log2(s.tokens) - np.log2(s.tokens).mean()) / np.log2(s.tokens).std()
            for target, data, formula in (
                ("correct", s, "correct ~ log2_tokens_z + resolved + C(agent_family)"),
                ("false_positive", s[s.resolved == 0], "pred ~ log2_tokens_z + C(agent_family)"),
            ):
                try:
                    groups = pd.factorize(data["pair_id"])[0]
                    fit = smf.logit(formula, data=data).fit(disp=0, cov_type="cluster", cov_kwds={"groups": groups})
                    b, (lo, hi) = fit.params["log2_tokens_z"], fit.conf_int().loc["log2_tokens_z"]
                    rows.append({"section": "length_regression", "judge": jn, "condition": cond, "group": target,
                                 "metric": "odds_ratio_per_sd_log_tokens", "value": np.exp(b), "ci_lo": np.exp(lo),
                                 "ci_hi": np.exp(hi), "n": len(data)})
                    rows.append({"section": "length_regression", "judge": jn, "condition": cond, "group": target,
                                 "metric": "p_value", "value": fit.pvalues["log2_tokens_z"], "ci_lo": np.nan,
                                 "ci_hi": np.nan, "n": len(data)})
                except Exception as e:  # perfect separation etc. (e.g. a judge that always says success)
                    rows.append({"section": "length_regression", "judge": jn, "condition": cond, "group": target,
                                 "metric": f"not_estimable: {type(e).__name__}", "value": np.nan, "ci_lo": np.nan,
                                 "ci_hi": np.nan, "n": len(data)})

    m = pd.DataFrame(rows)
    m.to_csv(os.path.join(ROOT, "results", "metrics.csv"), index=False)
    write_summary(df, m, judges, conds, cuts, seed, args.n_boot)
    write_error_candidates(df)
    print(f"wrote results/metrics.csv ({len(m)} rows), results/summary.md, results/predictions.csv, "
          f"results/error_candidates.md")


def fmt(m, section, judge, cond, metric, group="all", digits=2):
    r = m[(m.section == section) & (m.judge == judge) & (m.condition == cond) & (m.group == group) & (m.metric == metric)]
    if not len(r):
        return "–"
    r = r.iloc[0]
    if np.isnan(r.ci_lo):
        return f"{r.value:.{digits}f}"
    return f"{r.value:.{digits}f} [{r.ci_lo:.{digits}f}, {r.ci_hi:.{digits}f}]"


def write_summary(df, m, judges, conds, cuts, seed, n_boot):
    pv = sorted(set(df.prompt_version) - {""})
    L = [f"# Judge reliability results",
         "",
         f"Generated {dt.datetime.now().isoformat(timespec='minutes')} · items: {df.item_id.nunique()} "
         f"({df.pair_id.nunique()} pairs, 50/50 resolved) · prompt version(s): {', '.join(pv)} · "
         f"bootstrap: {n_boot} pair resamples, seed {seed} · 95% CIs in brackets.",
         "",
         "## Coverage", "",
         "| judge | condition | ok | parse fail | missing |", "|---|---|---|---|---|"]
    for jn in judges:
        for c in conds:
            L.append(f"| {jn} | {c} | {fmt(m,'coverage',jn,c,'n_ok',digits=0)} | "
                     f"{fmt(m,'coverage',jn,c,'parse_fail',digits=0)} | {fmt(m,'coverage',jn,c,'missing',digits=0)} |")
    L += ["", "## Main table (RQ1, RQ2)", "",
          "| judge | condition | balanced acc. | kappa | FPR | FNR | says success | mean conf. |",
          "|---|---|---|---|---|---|---|---|"]
    for jn in judges:
        for c in conds:
            L.append(f"| {jn} | {c} | {fmt(m,'overall',jn,c,'balanced_accuracy')} | {fmt(m,'overall',jn,c,'kappa')} | "
                     f"{fmt(m,'overall',jn,c,'fpr')} | {fmt(m,'overall',jn,c,'fnr')} | "
                     f"{fmt(m,'overall',jn,c,'success_rate')} | {fmt(m,'overall',jn,c,'mean_confidence')} |")
    L += ["", "## Full trajectory vs final patch only (paired; positive = higher with trajectory)", "",
          "| judge | Δ says success | Δ FPR | Δ balanced acc. |", "|---|---|---|---|"]
    for jn in judges:
        L.append(f"| {jn} | {fmt(m,'condition_delta',jn,'full-final_only','delta_success_rate')} | "
                 f"{fmt(m,'condition_delta',jn,'full-final_only','delta_fpr')} | "
                 f"{fmt(m,'condition_delta',jn,'full-final_only','delta_balanced_accuracy')} |")
    L += ["", "## Pair discrimination (same instance, one resolved + one unresolved trajectory)", "",
          "| judge | condition | correctly separated | reversed |", "|---|---|---|---|"]
    for jn in judges:
        for c in conds:
            L.append(f"| {jn} | {c} | {fmt(m,'pairs',jn,c,'pair_separated')} | {fmt(m,'pairs',jn,c,'pair_reversed')} |")
    L += ["", "## Inter-judge agreement (Krippendorff's alpha, nominal)", ""]
    for c in conds:
        L.append(f"- {c}: {fmt(m,'agreement','all_judges',c,'krippendorff_alpha')}")
    L += ["", f"## By trajectory length tertile (RQ3) — cuts at {cuts[0]:,.0f} and {cuts[1]:,.0f} tokens", "",
          "| judge | condition | BA T1 | BA T2 | BA T3 | FPR T1 | FPR T2 | FPR T3 |", "|---|---|---|---|---|---|---|---|"]
    for jn in judges:
        for c in conds:
            L.append(f"| {jn} | {c} | " + " | ".join(fmt(m, 'length_tertile', jn, c, 'balanced_accuracy', f'T{t}') for t in (1, 2, 3))
                     + " | " + " | ".join(fmt(m, 'length_tertile', jn, c, 'fpr', f'T{t}') for t in (1, 2, 3)) + " |")
    L += ["", "Length effect controlling for agent family (logistic regression; odds ratio per +1 SD of log "
          "trajectory tokens; pair-clustered SEs):", "",
          "| judge | condition | P(correct) OR | p | P(false positive) OR | p |", "|---|---|---|---|---|---|"]
    for jn in judges:
        for c in conds:
            L.append(f"| {jn} | {c} | {fmt(m,'length_regression',jn,c,'odds_ratio_per_sd_log_tokens','correct')} | "
                     f"{fmt(m,'length_regression',jn,c,'p_value','correct',3)} | "
                     f"{fmt(m,'length_regression',jn,c,'odds_ratio_per_sd_log_tokens','false_positive')} | "
                     f"{fmt(m,'length_regression',jn,c,'p_value','false_positive',3)} |")
    L += ["", "## By agent submission (balanced accuracy / FPR)", ""]
    fams = sorted(df.agent_family.unique())
    L += ["| judge | condition | " + " | ".join(fams) + " |", "|---|---|" + "---|" * len(fams)]
    for jn in judges:
        for c in conds:
            L.append(f"| {jn} | {c} | " + " | ".join(
                f"{fmt(m,'agent_family',jn,c,'balanced_accuracy',f)} / {fmt(m,'agent_family',jn,c,'fpr',f)}".replace(" [", "<br>[")
                for f in fams) + " |")
    L += ["", "## RQ4 (exploratory): judge vs same-family agent", "",
          "| judge | condition | FPR same family | FPR other | BA same family | BA other |", "|---|---|---|---|---|---|"]
    for jn in judges:
        if not df[(df.judge == jn)].same_family.any():
            continue
        for c in conds:
            L.append(f"| {jn} | {c} | {fmt(m,'self_preference',jn,c,'fpr','same_family')} | "
                     f"{fmt(m,'self_preference',jn,c,'fpr','other_family')} | "
                     f"{fmt(m,'self_preference',jn,c,'balanced_accuracy','same_family')} | "
                     f"{fmt(m,'self_preference',jn,c,'balanced_accuracy','other_family')} |")
    with open(os.path.join(ROOT, "results", "summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


def write_error_candidates(df):
    """High-confidence false positives, ranked by how many judges made the same mistake."""
    fp = df[(df.resolved == 0) & (df.pred == 1)]
    n_judges_fp = fp.groupby(["item_id", "condition"]).judge.nunique().rename("n_judges_fp")
    fp = fp.join(n_judges_fp, on=["item_id", "condition"])
    fp = fp[fp.confidence >= 0.9].sort_values(["n_judges_fp", "confidence"], ascending=False)
    L = ["# High-confidence false positives (candidates for qualitative analysis)", "",
         "Unresolved trajectories judged `success` with confidence >= 0.9, most-shared mistakes first.", ""]
    for _, r in fp.head(60).iterrows():
        L += [f"### {r.item_id} · {r.instance_id} · agent={r.agent_family} · judge={r.judge} · {r.condition} · "
              f"conf={r.confidence:.2f} · {r.n_judges_fp} judge(s) wrong",
              "", r.rationale, ""]
    with open(os.path.join(ROOT, "results", "error_candidates.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))


if __name__ == "__main__":
    main()

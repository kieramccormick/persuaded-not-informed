"""Build data/sample.jsonl: matched resolved/unresolved pairs on shared instances.

Design (see DATA_NOTES.md): an instance is eligible if the six submissions disagree on it
(1-5 of 6 resolved). We draw `n_pairs` eligible instances and, for each, pick one submission
that resolved it and one that did not, greedily balancing per-submission counts. Every pair
therefore holds the task fixed, and the sample is exactly 50/50 resolved overall.

Usage:
  python src/sample.py                     # select, download needed trajs, normalize, write
  python src/sample.py --select-only       # just write data/selection.jsonl
"""

import argparse
import collections
import hashlib
import importlib.metadata as md
import json
import os
import platform
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_data  # noqa: E402
import normalize  # noqa: E402

ROOT = fetch_data.ROOT


def load_labels(cfg):
    """{sub: {iid: resolved}} from per-instance report.json; instances without a report are absent."""
    raw = cfg["data"]["raw_dir"]
    labels, sizes = {}, {}
    for sub in cfg["data"]["submissions"]:
        name = sub["name"]
        with open(os.path.join(raw, name, "manifest.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        labels[name], sizes[name] = {}, {}
        for key, size in manifest.items():
            parts = key.split("/")
            if parts[0] == "trajs" and key.endswith(".traj.json"):
                sizes[name][parts[1]] = size
            elif parts[0] == "logs" and parts[-1] == "report.json":
                with open(os.path.join(raw, name, key), encoding="utf-8") as f:
                    labels[name][parts[1]] = bool(json.load(f)["resolved"])
    return labels, sizes


def select(cfg, labels, sizes, log):
    seed, n_pairs = cfg["sample"]["seed"], cfg["sample"]["n_pairs"]
    max_bytes = cfg["data"]["max_raw_traj_mb"] * 1e6
    subs = [s["name"] for s in cfg["data"]["submissions"]]

    def empty_patch(s, i):
        with open(os.path.join(cfg["data"]["raw_dir"], s, "logs", i, "patch.diff"), encoding="utf-8") as f:
            return not f.read().strip()

    drop_empty = cfg["sample"]["exclude_empty_patch"]
    usable = {
        s: {i for i in labels[s] if i in sizes[s] and sizes[s][i] <= max_bytes and not (drop_empty and empty_patch(s, i))}
        for s in subs
    }
    log["excluded_oversize"] = sorted(f"{s}/{i}" for s in subs for i in labels[s] if sizes[s].get(i, 0) > max_bytes)
    log["excluded_empty_patch"] = sorted(f"{s}/{i}" for s in subs for i in labels[s] if drop_empty and empty_patch(s, i))
    log["excluded_no_report"] = sorted(f"{s}/{i}" for s in subs for i in sizes[s] if i not in labels[s])
    def options(iid):
        avail = [s for s in subs if iid in usable[s]]
        return [s for s in avail if labels[s][iid]], [s for s in avail if not labels[s][iid]]

    # Eligible: at least one usable resolved and one usable unresolved trajectory.
    all_iids = sorted(set().union(*usable.values()))
    mixed = [i for i in all_iids if all(options(i))]
    log["n_instances_mixed"] = len(mixed)
    if n_pairs > len(mixed):
        sys.exit(f"n_pairs={n_pairs} exceeds eligible instances ({len(mixed)})")

    rng = random.Random(seed)
    chosen = rng.sample(mixed, n_pairs)
    order = {iid: k for k, iid in enumerate(chosen)}

    # Most-constrained instances first so the greedy balance has room to work.
    chosen.sort(key=lambda i: (min(map(len, options(i))), order[i]))
    n_res, n_unres = collections.Counter(), collections.Counter()
    rows = []
    for iid in chosen:
        res, unres = options(iid)
        r = min(res, key=lambda s: (n_res[s] + n_unres[s], n_res[s], rng.random()))
        n_res[r] += 1
        u = min(unres, key=lambda s: (n_res[s] + n_unres[s], n_unres[s], rng.random()))
        n_unres[u] += 1
        pair_id = f"p{order[iid]:03d}"
        rows.append({"pair_id": pair_id, "instance_id": iid, "submission": r, "resolved": True})
        rows.append({"pair_id": pair_id, "instance_id": iid, "submission": u, "resolved": False})
    rows.sort(key=lambda x: (x["pair_id"], not x["resolved"]))
    return rows


def build(cfg, rows, log):
    raw, ncfg = cfg["data"]["raw_dir"], cfg["normalize"]
    subs = {s["name"]: s for s in cfg["data"]["submissions"]}
    issues = {}
    with open(os.path.join(raw, "hf", "verified.jsonl"), encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            issues[d["instance_id"]] = d

    out, problems = [], collections.Counter()
    for row in rows:
        name, iid = row["submission"], row["instance_id"]
        traj_path = os.path.join(raw, name, "trajs", iid, f"{iid}.traj.json")
        with open(traj_path, encoding="utf-8") as f:
            traj = json.load(f)
        if traj.get("instance_id") not in (None, iid):
            raise ValueError(f"instance id mismatch in {traj_path}")
        steps, meta = normalize.parse(traj)
        kw = dict(max_tokens=ncfg["max_traj_tokens"], max_obs_tokens=ncfg["max_obs_tokens"], enc=ncfg["tokenizer"])
        text, stats = normalize.render(steps, agent_text=False, **kw)
        text_at, stats_at = normalize.render(steps, agent_text=True, **kw)

        with open(os.path.join(raw, name, "logs", iid, "patch.diff"), encoding="utf-8") as f:
            patch = f.read()
        if patch.strip() != meta["submission"].strip():
            problems["patch_differs_from_traj_submission"] += 1
        if meta["exit_status"] != "Submitted":
            problems[f"exit_status={meta['exit_status']}"] += 1
        if not patch.strip():
            problems["empty_patch"] += 1

        primary = text_at if ncfg["include_agent_text"] else text
        out.append({
            "item_id": f"{row['pair_id']}-{'R' if row['resolved'] else 'U'}",
            "pair_id": row["pair_id"],
            "instance_id": iid,
            "repo": issues[iid]["repo"],
            "submission": name,
            "agent_family": subs[name]["family"],
            "agent_model": subs[name]["model"],
            "resolved": row["resolved"],
            "issue_text": issues[iid]["problem_statement"],
            "final_patch": patch,
            "trajectory": primary,
            "trajectory_with_agent_text": text_at,
            "exit_status": meta["exit_status"],
            "length": {
                "n_turns": meta["n_turns"],
                "n_actions": meta["n_actions"],
                "n_env_errors": meta["n_env_errors"],
                "agent_text_chars": meta["agent_text_chars"],
                "reasoning_chars": meta["reasoning_chars"],
                "patch_tokens": normalize.n_tokens(patch, ncfg["tokenizer"]),
                "issue_tokens": normalize.n_tokens(issues[iid]["problem_statement"], ncfg["tokenizer"]),
                **stats,
                "with_agent_text": stats_at,
            },
        })
    log["data_problems"] = dict(problems)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--select-only", action="store_true")
    args = ap.parse_args()
    cfg = fetch_data.load_config()
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)

    with open(os.path.join(ROOT, "config.yaml"), "rb") as f:
        cfg_hash = hashlib.sha256(f.read()).hexdigest()[:12]
    with open(os.path.join(cfg["data"]["raw_dir"], "hf", "verified.jsonl.revision")) as f:
        hf_rev = f.read().strip()
    log = {
        "seed": cfg["sample"]["seed"],
        "config_sha256_12": cfg_hash,
        "experiments_commit": cfg["data"]["experiments_commit"],
        "hf_dataset": f"{cfg['data']['hf_dataset']}@{hf_rev}",
        "python": platform.python_version(),
        "packages": {p: md.version(p) for p in ("tiktoken", "boto3", "pyyaml", "datasets")},
    }

    labels, sizes = load_labels(cfg)
    rows = select(cfg, labels, sizes, log)
    with open(os.path.join(ROOT, "data", "selection.jsonl"), "w", encoding="utf-8") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)
    if args.select_only:
        print(json.dumps(log, indent=1))
        return

    ids_path = os.path.join(ROOT, "data", "needed_trajs.tsv")
    with open(ids_path, "w", encoding="utf-8") as f:
        f.writelines(f"{r['submission']}\t{r['instance_id']}\n" for r in rows)
    fetch_data.step_trajs(cfg, fetch_data.s3_client(), ids_path)

    items = build(cfg, rows, log)
    with open(os.path.join(ROOT, "data", "sample.jsonl"), "w", encoding="utf-8") as f:
        f.writelines(json.dumps(x, ensure_ascii=False) + "\n" for x in items)
    log["n_items"] = len(items)
    with open(os.path.join(ROOT, "data", "sample_log.json"), "w", encoding="utf-8") as f:
        json.dump(log, f, indent=1)
    print(json.dumps({k: v for k, v in log.items() if not k.startswith("excluded")}, indent=1))
    print("excluded_oversize:", log["excluded_oversize"], "| excluded_no_report:", log["excluded_no_report"])


if __name__ == "__main__":
    main()

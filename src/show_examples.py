"""Render sample items as readable Markdown pages: issue, hidden-test outcome, patch,
trajectory (exactly as the judges saw it) and every judge's verdict.

Usage:
  python src/show_examples.py --pairs p002 p004 p008     # both items of each pair
  python src/show_examples.py --items p002-U             # single items
Writes examples/<item_id>.md
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_data  # noqa: E402
import judge as judge_mod  # noqa: E402

ROOT = fetch_data.ROOT


def test_section(cfg, item):
    path = os.path.join(cfg["data"]["raw_dir"], item["submission"], "logs", item["instance_id"], "report.json")
    with open(path, encoding="utf-8") as f:
        ts = json.load(f)["tests_status"]
    out = []
    for kind, desc in (("FAIL_TO_PASS", "tests added by the real fix; must now pass"),
                       ("PASS_TO_PASS", "existing tests; must keep passing")):
        ok, bad = ts[kind]["success"], ts[kind]["failure"]
        out.append(f"**{kind}** ({desc}): {len(ok)} passed, {len(bad)} failed")
        for t in bad[:15]:
            out.append(f"- FAILED: `{t}`")
        if len(bad) > 15:
            out.append(f"- ... and {len(bad) - 15} more")
        if kind == "FAIL_TO_PASS":
            for t in ok[:5]:
                out.append(f"- passed: `{t}`")
    return "\n".join(out)


def render(cfg, item):
    verdicts = []
    for j in cfg["judge"]["judges"]:
        for cond in cfg["judge"]["conditions"]:
            p = judge_mod.cache_path(j["name"], cond, item["item_id"])
            if not os.path.exists(p):
                verdicts.append(f"| {j['name']} | {cond} | (missing) | | |")
                continue
            with open(p, encoding="utf-8") as f:
                r = json.load(f)
            res = r["result"]
            if not res:
                verdicts.append(f"| {j['name']} | {cond} | (parse failure) | | |")
                continue
            right = (res["verdict"] == "success") == item["resolved"]
            rat = res["rationale"].replace("|", "\\|").replace("\n", " ")
            verdicts.append(f"| {j['name']} | {cond} | **{res['verdict']}** {'✓' if right else '✗'} | "
                            f"{res['confidence']:.2f} | {rat} |")
    L = [f"# {item['item_id']}: {item['instance_id']}", "",
         f"- **Ground truth: {'RESOLVED' if item['resolved'] else 'UNRESOLVED'}**",
         f"- Agent: {item['agent_model']} ({item['agent_family']}) via mini-SWE-agent; submission `{item['submission']}`",
         f"- Trajectory: {item['length']['n_turns']} turns, {item['length']['tokens']:,} tokens; "
         f"pair partner: `{item['pair_id']}-{'U' if item['resolved'] else 'R'}`",
         "", "## Hidden test outcome (never shown to the agent or the judges)", "", test_section(cfg, item),
         "", "## Judge verdicts", "", "| judge | condition | verdict | confidence | rationale |",
         "|---|---|---|---|---|", *verdicts,
         "", "## Issue (shown to judges)", "", "````text", item["issue_text"].strip(), "````",
         "", "## Final patch (shown to judges)", "", "````diff", item["final_patch"].strip(), "````",
         "", "## Trajectory (shown to judges in the `full` condition only)", "", "````text",
         item["trajectory"], "````", ""]
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pairs", nargs="*", default=[])
    ap.add_argument("--items", nargs="*", default=[])
    args = ap.parse_args()
    cfg = fetch_data.load_config()
    with open(os.path.join(ROOT, "data", "sample.jsonl"), encoding="utf-8") as f:
        items = [json.loads(line) for line in f]
    want = set(args.items) | {f"{p}-{s}" for p in args.pairs for s in "RU"}
    os.makedirs(os.path.join(ROOT, "examples"), exist_ok=True)
    for item in items:
        if item["item_id"] in want:
            path = os.path.join(ROOT, "examples", f"{item['item_id']}.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(render(cfg, item))
            print("wrote", os.path.relpath(path, ROOT))


if __name__ == "__main__":
    main()

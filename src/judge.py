"""Run LLM judges over data/sample.jsonl.

Every response is cached at cache/<judge>/<condition>/<item_id>.json and a cached call is never
re-run, so the script can be interrupted and resumed. Calls that got no usable API response
(after retries) are logged to results/api_failures.jsonl instead of cached, so a rerun retries them. Malformed JSON is retried up to
`max_parse_retries` times; if every attempt fails the record is kept with parse_ok=false.

Usage:
  python src/judge.py --subset pilot --dry-run              # token/cost estimate, no API calls
  python src/judge.py --subset pilot --limit 1 --conditions final_only --budget-usd 0.10   # smoke test
  python src/judge.py --subset pilot --budget-usd 5         # run the pilot
  python src/judge.py --subset all --budget-usd 60          # full run
  python src/judge.py --subset pilot --judges glm-5 --conditions full

Reads API keys from the environment variables named in config.yaml (OPENROUTER_API_KEY for
OpenRouter judges, OPENAI_API_KEY for direct OpenAI judges). Keys are never written to the cache.
"""

import argparse
import collections
import datetime as dt
import hashlib
import json
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_data  # noqa: E402
import normalize  # noqa: E402

ROOT = fetch_data.ROOT


# ---------------------------------------------------------------- prompts

def load_prompts(jcfg):
    with open(os.path.join(ROOT, jcfg["prompt"]), encoding="utf-8") as f:
        template = f.read()
    with open(os.path.join(ROOT, jcfg["trajectory_block"]), encoding="utf-8") as f:
        block = f.read()
    version = hashlib.sha256((template + "\0" + block).encode()).hexdigest()[:12]
    return template, block, version


def build_prompt(item, condition, template, block):
    if condition == "full":
        traj = block.replace("{trajectory}", item["trajectory"])
    elif condition == "final_only":
        traj = ""
    else:
        raise ValueError(condition)
    return (template.replace("{issue}", item["issue_text"].strip())
            .replace("{trajectory_block}", traj)
            .replace("{patch}", item["final_patch"].strip()))


# ---------------------------------------------------------------- subsets

def pilot_ids(items, n_pairs, seed):
    """n_pairs pairs, stratified by length tertile of the pair's mean trajectory tokens."""
    pairs = collections.defaultdict(list)
    for x in items:
        pairs[x["pair_id"]].append(x)
    mean_len = {p: sum(x["length"]["tokens"] for x in xs) / len(xs) for p, xs in pairs.items()}
    ordered = sorted(pairs, key=lambda p: (mean_len[p], p))
    k = len(ordered)
    tertiles = [ordered[: k // 3], ordered[k // 3: 2 * k // 3], ordered[2 * k // 3:]]
    rng = random.Random(seed)
    quota = [n_pairs // 3 + (1 if i < n_pairs % 3 else 0) for i in range(3)]
    chosen = []
    for t, q in zip(tertiles, quota):
        chosen += rng.sample(t, q)
    return sorted(x["item_id"] for p in chosen for x in pairs[p])


def load_items(subset, seed, n_pairs):
    with open(os.path.join(ROOT, "data", "sample.jsonl"), encoding="utf-8") as f:
        items = [json.loads(line) for line in f]
    if subset == "all":
        return items
    ids = set(pilot_ids(items, n_pairs, seed))
    with open(os.path.join(ROOT, "data", "pilot_ids.txt"), "w", encoding="utf-8") as f:
        f.writelines(i + "\n" for i in sorted(ids))
    return [x for x in items if x["item_id"] in ids]


# ---------------------------------------------------------------- parsing

def parse_verdict(text):
    """Return (parsed_dict, error). Accepts bare JSON, fenced JSON, or JSON embedded in prose."""
    if not text:
        return None, "empty response"
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    candidates = [s] + re.findall(r"\{.*\}", s, re.S)
    for c in candidates:
        try:
            d = json.loads(c)
        except json.JSONDecodeError:
            continue
        if not isinstance(d, dict):
            continue
        verdict = str(d.get("verdict", "")).strip().lower()
        if verdict not in ("success", "failure"):
            return None, f"bad verdict: {d.get('verdict')!r}"
        try:
            conf = float(d.get("confidence"))
        except (TypeError, ValueError):
            return None, f"bad confidence: {d.get('confidence')!r}"
        if not 0 <= conf <= 1:
            return None, f"confidence out of range: {conf}"
        return {"verdict": verdict, "confidence": conf, "rationale": str(d.get("rationale", "")).strip()}, None
    return None, "no JSON object found"


# ---------------------------------------------------------------- API

class BudgetExceeded(Exception):
    pass


class Runner:
    def __init__(self, jcfg, judges, budget_usd):
        self.jcfg = jcfg
        self.clients = {}
        apis = {
            "openrouter": (jcfg["api_base"], jcfg["api_key_env"], 600.0),
            "openai": (jcfg["openai_api_base"], jcfg["openai_api_key_env"], 900.0),  # flex can be slow
        }
        for api in {j["api"] for j in judges}:
            base, env, timeout = apis[api]
            key = os.environ.get(env)
            if not key:
                sys.exit(f"environment variable {env} is not set (needed for {api} judges)")
            headers = {"Authorization": f"Bearer {key}"}
            if api == "openrouter":
                headers["X-Title"] = "AES judge reliability study"
            self.clients[api] = httpx.Client(base_url=base, headers=headers,
                                             timeout=httpx.Timeout(timeout, connect=30.0))
        self.budget = budget_usd
        self.spent = 0.0
        self.spent_by_judge = collections.Counter()
        self.lock = threading.Lock()

    def request_body(self, judge, prompt):
        body = {
            "model": judge["model"],
            "messages": [{"role": "user", "content": prompt}],
            "seed": self.jcfg["seed"],
        }
        if judge["api"] == "openrouter":
            body.update({
                "max_tokens": self.jcfg["max_tokens"],
                "reasoning": {"effort": self.jcfg["reasoning_effort"]},
                "usage": {"include": True},
            })
            # `only` already restricts the provider set; fallbacks are allowed *within* it
            # (with allow_fallbacks=false a busy first provider fails instead of trying the next).
            body["provider"] = dict(judge.get("provider") or {}) | {"require_parameters": True}
        else:  # openai direct
            body.update({
                "max_completion_tokens": self.jcfg["max_tokens"],
                "reasoning_effort": self.jcfg["reasoning_effort"],
            })
            if judge.get("service_tier"):
                body["service_tier"] = judge["service_tier"]
        if judge.get("json_mode"):
            body["response_format"] = {"type": "json_object"}
        if judge.get("temperature") is not None:
            body["temperature"] = judge["temperature"]
        return body

    def cost(self, judge, usage):
        """OpenRouter reports cost; for OpenAI direct compute it from token counts."""
        if judge["api"] == "openrouter":
            return float(usage.get("cost") or 0.0)
        p = judge["price"]
        return (usage.get("prompt_tokens", 0) * p["input"] + usage.get("completion_tokens", 0) * p["output"]) / 1e6

    def post(self, judge, body):
        """POST with retries on 429 / 5xx / network errors. Returns (json, latency_s, error)."""
        delay = 5.0
        cap = judge.get("max_spend_usd")
        for attempt in range(self.jcfg["max_api_retries"] + 1):
            with self.lock:
                if self.spent >= self.budget:
                    raise BudgetExceeded(f"spent ${self.spent:.2f} >= budget ${self.budget:.2f}")
                if cap is not None and self.spent_by_judge[judge["name"]] >= cap:
                    raise BudgetExceeded(f"{judge['name']} spent ${self.spent_by_judge[judge['name']]:.2f} >= cap ${cap:.2f}")
            t0 = time.time()
            try:
                r = self.clients[judge["api"]].post("/chat/completions", json=body)
                latency = time.time() - t0
                if r.status_code == 200:
                    data = r.json()
                    if "error" in data:  # OpenRouter can return 200 with an upstream error
                        err = data["error"]
                        if attempt < self.jcfg["max_api_retries"]:
                            time.sleep(delay + random.random())
                            delay = min(delay * 2, 120)
                            continue
                        return None, latency, f"upstream error: {json.dumps(err)[:500]}"
                    return data, latency, None
                if r.status_code in (408, 429, 500, 502, 503, 504) and attempt < self.jcfg["max_api_retries"]:
                    retry_after = float(r.headers.get("retry-after", 0) or 0)
                    time.sleep(max(delay, retry_after) + random.random())
                    delay = min(delay * 2, 120)
                    continue
                return None, latency, f"HTTP {r.status_code}: {r.text[:500]}"
            except httpx.HTTPError as e:
                if attempt < self.jcfg["max_api_retries"]:
                    time.sleep(delay + random.random())
                    delay = min(delay * 2, 120)
                    continue
                return None, time.time() - t0, f"{type(e).__name__}: {e}"
        return None, 0.0, "retries exhausted"

    def judge_one(self, judge, condition, item, prompt, prompt_version):
        body = self.request_body(judge, prompt)
        attempts, parsed = [], None
        for _ in range(1 + self.jcfg["max_parse_retries"]):
            data, latency, err = self.post(judge, body)
            rec = {"latency_s": round(latency, 2), "error": err}
            if data is not None:
                choice = (data.get("choices") or [{}])[0]
                msg = choice.get("message") or {}
                usage = data.get("usage") or {}
                cost = self.cost(judge, usage)
                with self.lock:
                    self.spent += cost
                    self.spent_by_judge[judge["name"]] += cost
                rec.update({
                    "content": msg.get("content"),
                    "reasoning": msg.get("reasoning"),
                    "finish_reason": choice.get("finish_reason"),
                    "provider": data.get("provider") or judge["api"],
                    "service_tier": data.get("service_tier"),
                    "model_served": data.get("model"),
                    "generation_id": data.get("id"),
                    "usage": usage,
                    "cost_usd": cost,
                })
                parsed, perr = parse_verdict(msg.get("content"))
                rec["parse_error"] = perr
            attempts.append(rec)
            if parsed is not None or data is None:
                break
        return {
            "judge": judge["name"],
            "model": judge["model"],
            "condition": condition,
            "item_id": item["item_id"],
            "prompt_version": prompt_version,
            "request": {k: v for k, v in body.items() if k != "messages"},
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "attempts": attempts,
            "parse_ok": parsed is not None,
            "api_ok": attempts[-1]["error"] is None,
            "result": parsed,
            "cost_usd": sum(a.get("cost_usd", 0.0) for a in attempts),
        }


# ---------------------------------------------------------------- main

def cache_path(judge_name, condition, item_id):
    return os.path.join(ROOT, "cache", judge_name, condition, f"{item_id}.json")


def dry_run(jobs, judges, assumed_output):
    print(f"{'judge':15s} {'condition':11s} {'calls':>5s} {'input tok':>11s} {'est. $':>8s}")
    total = 0.0
    agg = collections.defaultdict(lambda: [0, 0])
    for judge_name, condition, _, prompt, cached in jobs:
        if cached:
            continue
        a = agg[(judge_name, condition)]
        a[0] += 1
        a[1] += normalize.n_tokens(prompt)
    for (jn, cond), (n, tok) in sorted(agg.items()):
        p = judges[jn]["price"]
        usd = tok / 1e6 * p["input"] + n * assumed_output / 1e6 * p["output"]
        total += usd
        print(f"{jn:15s} {cond:11s} {n:5d} {tok:11,d} {usd:8.2f}")
    print(f"total (uncached calls, assuming {assumed_output} output tokens/call incl. reasoning): ${total:.2f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--subset", choices=["pilot", "all"], required=True)
    ap.add_argument("--judges", nargs="*", help="judge names (default: all in config)")
    ap.add_argument("--conditions", nargs="*", help="default: all in config")
    ap.add_argument("--dry-run", action="store_true", help="estimate tokens and cost; no API calls")
    ap.add_argument("--assumed-output-tokens", type=int, default=1500)
    ap.add_argument("--budget-usd", type=float, help="stop issuing calls once this run's actual spend reaches this")
    ap.add_argument("--limit", type=int, help="only the N shortest items of the subset (smoke tests)")
    args = ap.parse_args()

    cfg = fetch_data.load_config()
    jcfg = cfg["judge"]
    judges = {j["name"]: j for j in jcfg["judges"]}
    names = args.judges or list(judges)
    conditions = args.conditions or jcfg["conditions"]
    template, block, prompt_version = load_prompts(jcfg)
    items = load_items(args.subset, cfg["sample"]["seed"], jcfg["pilot_pairs"])
    if args.limit:
        items = sorted(items, key=lambda x: x["length"]["tokens"])[: args.limit]  # shortest first: cheap

    jobs, stale = [], []
    for jn in names:
        for cond in conditions:
            for item in items:
                path = cache_path(jn, cond, item["item_id"])
                cached = os.path.exists(path)
                if cached:
                    with open(path, encoding="utf-8") as f:
                        if json.load(f).get("prompt_version") != prompt_version:
                            stale.append(path)
                jobs.append((jn, cond, item, build_prompt(item, cond, template, block), cached))
    print(f"prompt_version={prompt_version} items={len(items)} judges={names} conditions={conditions} "
          f"calls={len(jobs)} cached={sum(j[4] for j in jobs)}")
    if stale:
        print(f"WARNING: {len(stale)} cached records use a different prompt version and will NOT be re-run, "
              f"e.g. {stale[0]}. Move them aside if the prompt change is intentional.")

    if args.dry_run:
        dry_run(jobs, judges, args.assumed_output_tokens)
        return
    if args.budget_usd is None:
        sys.exit("--budget-usd is required for a real run")

    runner = Runner(jcfg, [judges[jn] for jn in names], args.budget_usd)
    # Per-judge caps are cumulative across runs: count what earlier runs already spent.
    for jn in names:
        jdir = os.path.join(ROOT, "cache", jn)
        for dirpath, _, files in os.walk(jdir):
            for fn in files:
                if fn.endswith(".json"):
                    with open(os.path.join(dirpath, fn), encoding="utf-8") as f:
                        runner.spent_by_judge[jn] += json.load(f).get("cost_usd", 0.0)
    prior = {jn: round(v, 3) for jn, v in runner.spent_by_judge.items() if v}
    if prior:
        print(f"prior spend from cache: {prior}")
    start_by_judge = dict(runner.spent_by_judge)
    sems = {jn: threading.Semaphore(jcfg["concurrency_per_judge"]) for jn in names}
    # Interleave judges round-robin: the pool is FIFO, so a judge-ordered queue would park every
    # worker on the first judge's semaphore and run the judges one after another.
    by_judge = collections.defaultdict(list)
    for j in jobs:
        if not j[4]:
            by_judge[j[0]].append(j)
    queues = list(by_judge.values())
    todo = [q[k] for k in range(max(map(len, queues), default=0)) for q in queues if k < len(q)]
    log_path = os.path.join(ROOT, "results", "judge_runs.jsonl")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    fail_path = os.path.join(ROOT, "results", "api_failures.jsonl")
    fail_lock = threading.Lock()

    def work(job):
        jn, cond, item, prompt, _ = job
        with sems[jn]:
            rec = runner.judge_one(judges[jn], cond, item, prompt, prompt_version)
        if not rec["api_ok"]:
            # No usable response (rate limit, outage): log it but do not cache, so a later run retries.
            with fail_lock, open(fail_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            return rec
        path = cache_path(jn, cond, item["item_id"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".part", "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)
        os.replace(path + ".part", path)
        return rec

    stats = collections.Counter()
    budget_hit = None
    with ThreadPoolExecutor(max_workers=jcfg["concurrency_per_judge"] * len(names)) as ex:
        futures = [ex.submit(work, j) for j in todo]
        for k, fut in enumerate(as_completed(futures), 1):
            try:
                rec = fut.result()
            except BudgetExceeded as e:
                budget_hit = str(e)
                stats["skipped_budget"] += 1
                continue
            stats["done"] += 1
            stats["parse_fail"] += not rec["parse_ok"] and rec["api_ok"]
            stats["api_fail"] += not rec["api_ok"]
            if k % 20 == 0 or k == len(futures):
                print(f"  {k}/{len(futures)} spent=${runner.spent:.3f} {dict(stats)}", flush=True)

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "started": started, "finished": dt.datetime.now(dt.timezone.utc).isoformat(),
            "subset": args.subset, "judges": {jn: judges[jn]["model"] for jn in names}, "conditions": conditions,
            "prompt_version": prompt_version, "config_judge": jcfg, "stats": dict(stats),
            "spent_usd": round(runner.spent, 4), "budget_usd": args.budget_usd, "budget_hit": budget_hit,
        }) + "\n")
    print(f"done: {dict(stats)} spent this run=${runner.spent:.3f}; cumulative by judge: "
          + ", ".join(f"{k}=${v:.3f} (+{v - start_by_judge.get(k, 0):.3f})" for k, v in runner.spent_by_judge.items()) + (f" BUDGET STOP: {budget_hit}" if budget_hit else ""))


if __name__ == "__main__":
    main()

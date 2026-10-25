"""Fetch the raw artifacts needed for sampling.

Steps (run in order; each is idempotent and skips files already on disk):
  manifest  list every S3 object for each submission (key, size) -> raw/<sub>/manifest.json
  labels    download logs/<iid>/report.json + patch.diff for all instances
  issues    download SWE-bench Verified from Hugging Face -> raw/hf/verified.jsonl
  trajs     download trajectories for the instance ids listed in --ids (one per line, "sub<TAB>iid")

Usage:
  python src/fetch_data.py manifest labels issues
  python src/fetch_data.py trajs --ids data/needed_trajs.tsv
"""

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

import boto3
import yaml
from botocore import UNSIGNED
from botocore.config import Config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_config():
    with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def s3_client():
    return boto3.client("s3", config=Config(signature_version=UNSIGNED, max_pool_connections=32))


def step_manifest(cfg, s3):
    bucket = cfg["data"]["s3_bucket"]
    for sub in cfg["data"]["submissions"]:
        out = os.path.join(cfg["data"]["raw_dir"], sub["name"], "manifest.json")
        if os.path.exists(out):
            continue
        prefix = sub["s3_prefix"] + "/"
        objs = {}
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for o in page.get("Contents", []):
                objs[o["Key"][len(prefix):]] = o["Size"]
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(objs, f, indent=0, sort_keys=True)
        print(f"manifest {sub['name']}: {len(objs)} objects")


def _download(s3, bucket, key, dest):
    if os.path.exists(dest):
        return False
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    s3.download_file(bucket, key, tmp)
    os.replace(tmp, dest)
    return True


def _download_many(s3, bucket, jobs):
    with ThreadPoolExecutor(16) as ex:
        results = list(ex.map(lambda j: _download(s3, bucket, *j), jobs))
    return sum(results)


def step_labels(cfg, s3):
    bucket = cfg["data"]["s3_bucket"]
    for sub in cfg["data"]["submissions"]:
        base = os.path.join(cfg["data"]["raw_dir"], sub["name"])
        with open(os.path.join(base, "manifest.json"), encoding="utf-8") as f:
            manifest = json.load(f)
        jobs = [
            (f"{sub['s3_prefix']}/{k}", os.path.join(base, k))
            for k in manifest
            if k.startswith("logs/") and k.endswith(("/report.json", "/patch.diff"))
        ]
        n = _download_many(s3, bucket, jobs)
        print(f"labels {sub['name']}: {n} new of {len(jobs)} files")


def step_issues(cfg):
    out = os.path.join(cfg["data"]["raw_dir"], "hf", "verified.jsonl")
    if os.path.exists(out):
        return
    import datasets
    from huggingface_hub import HfApi

    name = cfg["data"]["hf_dataset"]
    revision = HfApi().dataset_info(name).sha
    ds = datasets.load_dataset(name, split="test", revision=revision)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        for row in ds:
            f.write(json.dumps({"instance_id": row["instance_id"], "repo": row["repo"],
                                "problem_statement": row["problem_statement"]}) + "\n")
    with open(out + ".revision", "w") as f:
        f.write(revision + "\n")
    print(f"issues: {len(ds)} rows from {name}@{revision}")


def step_trajs(cfg, s3, ids_path):
    bucket = cfg["data"]["s3_bucket"]
    subs = {s["name"]: s for s in cfg["data"]["submissions"]}
    jobs = []
    with open(ids_path, encoding="utf-8") as f:
        for line in f:
            name, iid = line.strip().split("\t")
            key = f"trajs/{iid}/{iid}.traj.json"
            jobs.append((f"{subs[name]['s3_prefix']}/{key}", os.path.join(cfg["data"]["raw_dir"], name, key)))
    n = _download_many(s3, bucket, jobs)
    print(f"trajs: {n} new of {len(jobs)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("steps", nargs="+", choices=["manifest", "labels", "issues", "trajs"])
    ap.add_argument("--ids", help="TSV of (submission, instance_id) for the trajs step")
    args = ap.parse_args()
    cfg = load_config()
    s3 = s3_client()
    for step in args.steps:
        if step == "manifest":
            step_manifest(cfg, s3)
        elif step == "labels":
            step_labels(cfg, s3)
        elif step == "issues":
            step_issues(cfg)
        elif step == "trajs":
            if not args.ids:
                sys.exit("trajs step needs --ids")
            step_trajs(cfg, s3, args.ids)


if __name__ == "__main__":
    main()

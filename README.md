# How reliable are LLM judges for agent trajectories?

Code and data pipeline for an AES Fall 2026 extended abstract. We measure how well LLM judges predict
**execution-verified** outcomes of SWE-bench Verified agent runs, and whether showing the judge the agent's full
trajectory (vs. only its final patch) helps.

**Headline result:** all four judges are at chance (balanced accuracy 0.49–0.56). They call 62–97% of failed runs
successful, and seeing the trajectory pushes verdicts further toward "success" without improving accuracy.
See `results/summary.md`.

## Design in brief

- **Agents:** six submissions from the same **mini-SWE-agent v2.0.0** batch on SWE-bench Verified (2026-02-17). They differ only in the model: Claude Sonnet 4.5, GPT-5.2, Gemini 3 Flash, DeepSeek-V3.2, GLM-5, Kimi K2.5.
- **Ground truth:** `resolved` from each instance's test-harness `report.json`.
- **Sample:** 126 **matched pairs**, 252 trajectories. Each pair is one resolved and one unresolved trajectory on the *same* instance, drawn from instances where the six agents disagree. The sample is 50/50 overall and 21/21 per agent. Seed `20261015`.
- **Judges:** DeepSeek-V3.2, Gemma-4-31B, Nemotron-3-Ultra (via OpenRouter) and GPT-5-mini (OpenAI API, Flex tier). Reasoning effort is "low" for all; temperature is 0 where supported (GPT-5-mini does not support it).
- **Conditions:**
  - `full`: issue + normalized trajectory + final patch.
  - `final_only`: issue + final patch.
  - One fixed rubric (`prompts/judge.txt`) for all judges and conditions. The judge never sees the ground truth or which model the agent was.
- **Metrics:**
  - per judge and condition: balanced accuracy, Cohen's kappa, FPR, FNR;
  - Krippendorff's alpha across judges;
  - length tertiles, plus a logistic regression controlling for agent family;
  - per-agent breakdown and an exploratory same-family analysis.
  - All CIs are 95% cluster bootstraps over pairs (1,000 resamples).

Details on the data source, formats, the license, and every exclusion are in **`DATA_NOTES.md`**.

## Repository layout

```
config.yaml              all settings: data source, submissions, normalization, sample, judges, prices
prompts/judge.txt        rubric prompt (identical for all judges and conditions)
prompts/trajectory_block.txt   trajectory section, included only in the `full` condition
src/fetch_data.py        download labels, patches, issue text, trajectories (public S3 + Hugging Face)
src/normalize.py         convert three provider-specific trajectory shapes to one text format
src/sample.py            build data/sample.jsonl (matched pairs)
src/judge.py             run judges with caching, retries and budget caps
src/metrics.py           compute metrics -> results/
src/figures.py           make the table and figure -> results/, figures/
src/show_examples.py     render items as readable pages -> examples/<item_id>.md
data/sample.jsonl        the 252-item sample (NOT in this repo; regenerate with src/sample.py, see license note)
data/selection.jsonl     the sampled (pair, instance, submission, label) rows: enough to rebuild the sample
data/sample_log.json     seed, dataset revisions, package versions, exclusions
data/pilot_ids.txt       the 20 pilot items
results/                 metrics.csv, summary.md, predictions.csv (every judge verdict, confidence and
                         rationale), table.tex/.md, error_examples.md
figures/                 fpr_by_length.png / .pdf
cache/                   raw judge responses (git-ignored)
```

## Reproducing

### 1. Environment

Python 3.12 with pinned dependencies:

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv -r requirements.txt
```

### 2. Data (no API keys needed)

Set `data.raw_dir` in `config.yaml` to a local folder with about 400 MB free. Then:

```bash
python src/fetch_data.py manifest labels issues   # S3 listings, report.json + patch.diff, HF issue text
python src/sample.py                              # selects pairs, downloads 252 trajectories, normalizes
```

`sample.py` is deterministic given the seed in `config.yaml`. It writes `data/sample.jsonl` and logs exclusions and
versions to `data/sample_log.json`.

### 3. Judges (paid API calls)

The script reads API keys **only** from environment variables and never writes them to disk:

| Variable | Used for |
|---|---|
| `OPENROUTER_API_KEY` | DeepSeek, Gemma, Nemotron |
| `OPENAI_API_KEY` | GPT-5-mini |

In PowerShell, set them without echoing:

```powershell
$s = Read-Host "OpenRouter key" -AsSecureString; $env:OPENROUTER_API_KEY = [Net.NetworkCredential]::new('', $s).Password
$s = Read-Host "OpenAI key" -AsSecureString; $env:OPENAI_API_KEY = [Net.NetworkCredential]::new('', $s).Password
```

Then run:

```bash
python src/judge.py --subset pilot --dry-run               # token and cost estimate, no calls
python src/judge.py --subset pilot --budget-usd 3          # 20-item pilot
python src/judge.py --subset all --budget-usd 8            # full run (2,016 calls)
```

How the runner behaves:

- **Resumable:** every response is cached under `cache/<judge>/<condition>/<item_id>.json`, and a cached call is never re-run.
- **Failures:** calls that fail at the API level (rate limits, outages) are logged to `results/api_failures.jsonl` and **not** cached. **Rerun the same command** until `api_fail` reaches 0.
- **Malformed JSON** is retried twice. If it still fails, it is kept as a parse failure.
- **Budget caps:** `--budget-usd` caps each run. `max_spend_usd` in `config.yaml` caps a judge's cumulative spend across runs.
- **Stopping:** on Windows, Ctrl+C may not stop a run. Close the terminal instead; this is safe.

**Cost and time of the reported run:** about $5.50 in total (DeepSeek $2.53, Gemma $1.83, GPT-5-mini $1.11, Nemotron free tier), and about 4 hours of wall time.

### 4. Analysis (no API calls)

```bash
python src/metrics.py      # results/metrics.csv, summary.md, predictions.csv, error_candidates.md
python src/figures.py      # results/table.tex/.md, figures/fpr_by_length.png/.pdf
```

## Notes and limitations

- **Contested instances by design.** Pairs come from instances where strong agents disagree. Accuracy here describes hard cases, not SWE-bench as a whole.
- **Trajectory rendering:** actions and observations only, by default. Model narration and reasoning are visible for some providers and absent for others (GPT-5.2 emits none), so including it would let judges identify the agent. A narration-included rendering is stored in `trajectory_with_agent_text` for an ablation. Model names are scrubbed, and tool-call format errors are reduced to a fixed sentence.
- **Exclusions:** trajectories with empty patches (23), a missing `report.json` (2) or raw size over 25 MB (1).
- **Missing judgments:** 8 of 2,016 (DeepSeek, full condition) were still rate-limited after reruns.
- **Provider pinning:** judges are pinned to specific OpenRouter providers for consistency. The provider that served each call is recorded in the cache. Gemma's pilot calls on the free tier are archived in `cache_archive/` and are not used.
- **Approximate keyword analysis:** "agent's own tests" themes in rationales (`results/error_examples.md`) come from keyword matching.

## Data license

The SWE-bench experiments repository has no LICENSE file. The trajectories were produced by the SWE-bench team
with the open-source mini-SWE-agent, and the issue text comes from public GitHub issues via SWE-bench Verified
(MIT). We report aggregate results and short quotes. **Ask the SWE-bench maintainers before redistributing
`data/sample.jsonl` in bulk.** See `DATA_NOTES.md`.

## Acknowledgments

The code in this repository was developed with the assistance of Claude Code (Anthropic). All study design
decisions and results were reviewed by the author.

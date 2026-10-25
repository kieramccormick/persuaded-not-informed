# Data notes

Inspected 2026-10-15. Source: `github.com/SWE-bench/experiments` at commit
`40f164d5b8f1d249bf95a6df8b74b577fd8e519d` (2026-09-03), plus the issue text from
`princeton-nlp/SWE-bench_Verified` on Hugging Face at revision `c104f840cc67f8b6eec6f759ebc8b2693d585d4a`.

## Where things live

The repo **no longer holds artifacts**, only entries. Each submission folder is
`evaluation/verified/<date>_<name>/` and contains:

| File | Contents |
|---|---|
| `metadata.yaml` | model, org, scaffold (`tags.agent`), `os_model`, and an `assets` block pointing to the artifacts |
| `per_instance_details.json` | (mini-SWE-agent entries) `{instance_id: {resolved, cost, api_calls}}` |
| `results/` | (older or non-mini entries) resolved-id lists and breakdowns |
| `README.md`, `logo.*` | description |

The artifacts live in one of two places:

* **Before mid-2026:** the public S3 bucket `s3://swe-bench-submissions/`, readable without credentials (unsigned boto3).
  * mini-SWE-agent "bash-only" runs are under `bash-only/<entry>/`.
  * Other submissions are under `verified/<entry>/`.
  * Use the exact path in `assets.trajs` / `assets.logs`. `analysis/download_logs.py` assumes `verified/...` and gets the bash-only prefix wrong.
* **From mid-2026:** a GitHub repo of the submitter's own, named by `assets.repo`.

S3 layout of a mini-SWE-agent v2 submission (`bash-only/<entry>/`):

| Path | What it holds |
|---|---|
| `trajs/<iid>/<iid>.traj.json` | **trajectory** (one per instance; median 0.2–1.2 MB raw) |
| `trajs/preds.json`, `trajs/minisweagent.log`, a run config | run-level files |
| `logs/<iid>/patch.diff` | **final patch** as evaluated |
| `logs/<iid>/report.json` | **resolved label** (top-level `resolved`, plus FAIL_TO_PASS / PASS_TO_PASS details) |
| `logs/<iid>/test_output.txt`, `job-output.json` | evaluation output |

`metadata.yaml` says `logs: null` for most 20260217 entries, but the logs are present in S3 anyway.

**Ground truth used:** `report.json["resolved"]`. It agreed with `per_instance_details.json` on all
2,998 instances that have a report across the six chosen submissions (0 disagreements).

## License

* `SWE-bench/experiments` has **no LICENSE file**, and GitHub reports `license: null`. Strictly, that means all rights reserved by default, although the README calls the logs "publicly accessible and meant to enable greater reproducibility and transparency."
* The submission artifacts (trajectories) are produced by the submitters. The six we use were run by the SWE-bench team with the open-source mini-SWE-agent (MIT).
* The SWE-bench Verified dataset is on Hugging Face (SWE-bench is MIT-licensed). The issue texts come from public GitHub issues.
* **Implication:** citing the repo and reporting aggregate results is clearly fine. Do not redistribute trajectories in bulk. Short quoted excerpts in the abstract should be fine. To redistribute `data/sample.jsonl`, ask the SWE-bench maintainers first.

## Trajectory formats across submissions

There is no required format; the README says "we purposely do not explicitly define reasoning traces".
Across the Verified board:

* **mini-SWE-agent entries** (about 50; the leaderboard's "bash only" view) share one JSON schema:
  `trajectory_format: mini-swe-agent-1.1`, with `messages[]` and `info.submission` / `info.exit_status`.
* **Other scaffolds** (OpenHands, SWE-agent, proprietary agents) each use their own formats: `.json`, `.md`, `.log` and so on. Several mix models from different families within one run (e.g. ACoder, Warp, Rovo Dev, SAGE), which makes the model family ambiguous.

Even within `mini-swe-agent-1.1`, assistant messages come in **three provider-specific shapes**:

| Shape | Models | Visible text | Reasoning | Actions | Observations |
|---|---|---|---|---|---|
| A: LiteLLM chat | Claude, Gemini | `content` (Gemini: always null) | `reasoning_content` / `thinking_blocks` | `tool_calls[].function.arguments` | `{role: tool}` |
| B: OpenRouter-style chat | DeepSeek, GLM, Kimi, MiniMax | `content` (DeepSeek: always empty) | `reasoning` / `reasoning_details` | `tool_calls[]` | `{role: tool}` |
| C: OpenAI Responses API | GPT-5.2 | none emitted | encrypted / empty | `output[type=function_call]` | `{type: function_call_output}` |

Other quirks:
* `{role: user}` messages after the task prompt are environment format-error feedback.
* Some GLM-5 and DeepSeek runs start with a malformed response that was not logged, so the trajectory opens with an error message that echoes part of the agent's text (sometimes including `</think>` tags).
* Every message carries provider metadata that would reveal the model: `extra.response.model`, `info.config.model`, tool-call id prefixes (`toolu_…`, `call_…`, `functions.bash:0`), Gemini `thought_signature`.

**Consequence for normalization:** how much narration a trajectory contains depends on the model family, not on the agent's behavior. GPT-5.2 has none at all, DeepSeek and Gemini have it only in reasoning fields, and Claude, GLM, Kimi and MiniMax have both. The **primary rendering is therefore actions + observations only**, which is uniform across families and carries no identifying style. A second rendering that includes narration (`trajectory_with_agent_text`) is stored for an optional ablation.

## Chosen submissions

All six are from the same **mini-SWE-agent v2.0.0** batch (2026-02-17). They share the scaffold, prompt, step limits and evaluation, so differences between them reflect the model only.

| Submission | Family | Model | Open weights | Resolved / 500 |
|---|---|---|---|---|
| `20260217_mini-v2.0.0_claude-4-5-sonnet-high` | Anthropic | claude-sonnet-4-5 | no | 357 |
| `20260217_mini-v2.0.0_gpt-5-2-high` | OpenAI | gpt-5.2 | no | 364 |
| `20260217_mini-v2.0.0_gemini-3-flash-high` | Google | gemini-3-flash | no | 379 |
| `20260217_mini-v2.0.0_deepseek-3-2-high` | DeepSeek | deepseek-v3.2 | yes | 350 |
| `20260217_mini-v2.0.0_glm-5-high` | Zhipu / Z.ai | glm-5 | yes | 364 |
| `20260217_mini-v2.0.0_kimi-k2-5-high` | Moonshot | kimi-k2.5 | yes | 354 |

Why these six:
* **One scaffold isolates model family**, which RQ4 needs, and gives one parser instead of N.
* **Complete artifacts:** 500/500 trajectories and patches each. Only Sonnet lacks two `report.json` files.
* **Similar resolve rates (70–76%)**, so no agent is trivially easy to judge.
* **Families line up with cheap judges.** Three open-weight families (DeepSeek, GLM, Kimi) can serve as cheap judges, and three closed families (Anthropic, OpenAI, Google) give the one closed judge a self-family match whichever is chosen.

Not chosen:
* `20260226_mini-v2.0.0_gemini-3-pro-high`: its `per_instance_details.json` marks **0/500 resolved** while metadata claims 69.6%. The labels are corrupted.
* `20260217_mini-v2.0.0_minimax-2-5-high`: a valid substitute if a seventh family is wanted.
* Claude Haiku 4.5 and Opus 4.5/4.6: a second Anthropic model adds no new family.

## Sample design (`src/sample.py`, seed 20261015)

* **Matched pairs:** 126 instances, each contributing one resolved and one unresolved trajectory from different agents, for 252 items. The task is held fixed within each pair, so judge errors cannot come from task difficulty. The sample is exactly 50/50 overall and 21 R / 21 U per submission (greedy balancing).
* **Eligible instances:** the six agents disagree on them. That gives 135 instances after exclusions, of which 126 are sampled.
  * Caveat: this pool leaves out instances every agent solves or every agent fails, so it leans toward medium-difficulty tasks.
* **Exclusions:**
  * raw trajectory over 25 MB (1);
  * no `report.json` (2);
  * **empty final patch** (23: 12 GLM-5, 5 Kimi, 4 Gemini, 1 Claude, 1 DeepSeek). Empty patches are trivially "failure", and 3 of the first 9 found were Gemini API outages or timeouts with 0–7 turns rather than agent behavior. Controlled by `sample.exclude_empty_patch` in `config.yaml`.
* After exclusions, every sampled trajectory ended with `exit_status: Submitted`, and every `patch.diff` matches `info.submission` exactly.

## Normalization (`src/normalize.py`)

* Drops the system prompt and task prompt (the judge gets the issue text separately) and the final `exit` message (the judge gets the patch separately).
* Renders each step as `### Step k`, then `[ACTION] $ cmd`, then `[OBSERVATION] <returncode>…<output>…`.
* `trajectory_with_agent_text` additionally shows `[AGENT]` narration (reasoning + visible text).
* Environment format-error feedback is collapsed to a fixed sentence ("Tool call error: the response contained no tool call." and so on). The raw message repeats the malformed response, including GLM's `</think><tool_call>` tags.
  * Residual signal: how often format errors occur still differs by family (Kimi 21/42 items, DeepSeek 22/42, GLM 7/42, Claude 2/42, GPT and Gemini 0). This is real agent behavior and is kept.
* Model and vendor names are replaced with `[MODEL]`. There were 0 hits in the sample. Provider metadata and tool-call IDs are never rendered.
* **Length caps:**
  * per observation: 4,000 tokens, cut from the middle (12 items affected);
  * per trajectory: 100,000 tokens, cutting whole steps from the middle and keeping both ends (0 items in the primary rendering, 1 in the agent-text rendering).
* Tokens are counted with tiktoken `o200k_base` as an approximation for all judges.

## Known data problems

* **Oversized trajectory:** GLM-5 `sphinx-doc__sphinx-9320` is 1.75 GB raw, apparently a runaway command output. All raw files over 25 MB are excluded from the pool; this is the only one among the chosen submissions.
* **Missing labels:** Sonnet `django__django-13315` and `django__django-14376` have no `report.json`. They are excluded from the pool.
* **Inconsistent versions:** directory names say mini v2.0.0, but `info.mini_version` inside the trajectories says 2.1.0.
* `analysis/download_logs.py` builds the wrong S3 prefix for bash-only entries (see above).

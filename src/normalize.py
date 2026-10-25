"""Normalize mini-SWE-agent trajectories into one judge-facing text format.

The six submissions share `trajectory_format: mini-swe-agent-1.1` but the assistant
messages come in three provider-specific shapes (see DATA_NOTES.md):
  A  LiteLLM chat (Claude, Gemini):          content / reasoning_content / tool_calls
  B  OpenRouter-style chat (DeepSeek, GLM, Kimi): content / reasoning / tool_calls
  C  OpenAI Responses API (GPT-5.2):         output[] items of type reasoning/message/function_call
Tool results are either {role: tool, content} (A, B) or {type: function_call_output, output} (C).

Each trajectory is parsed into steps, one per agent turn:
  Step = {text, reasoning, actions: [command], observations: [str], feedback: [str]}
and rendered as

  ### Step 3
  [ACTION] $ grep -n foo bar.py
  [OBSERVATION]
  <returncode>0</returncode> ...

The system prompt and the task prompt (messages 0-1) are dropped because the judge gets the
issue text separately; the final `exit` message is dropped because the judge gets the patch
separately. Provider metadata (model names, tool-call ids, signatures) is never rendered.

Usage (inspect one file):
  python src/normalize.py path/to/x.traj.json [--agent-text] [--max-tokens N]
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field

import tiktoken

_ENC = {}


def encoder(name="o200k_base"):
    if name not in _ENC:
        _ENC[name] = tiktoken.get_encoding(name)
    return _ENC[name]


def n_tokens(text, enc="o200k_base"):
    return len(encoder(enc).encode(text, disallowed_special=()))


# Model / vendor names that could reveal the agent's identity to the judge.
IDENTITY_RE = re.compile(
    r"\b(claude|anthropic|sonnet|chatgpt|gpt-?[0-9][\w.\-]*|openai|o[134]-mini|gemini|deepmind|bard|"
    r"deepseek|glm-?[0-9][\w.\-]*|zhipu|chatglm|z\.ai|kimi|moonshot|minimax|qwen)\b",
    re.IGNORECASE,
)


def scrub(text):
    """Replace model/vendor names with [MODEL]. Returns (text, n_replacements)."""
    return IDENTITY_RE.subn("[MODEL]", text)


@dataclass
class Step:
    text: str = ""
    reasoning: str = ""
    actions: list = field(default_factory=list)
    observations: list = field(default_factory=list)
    feedback: list = field(default_factory=list)  # environment messages, e.g. format errors


def _as_text(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") in ("text", "output_text")
        )
    return str(content)


def _command(arguments):
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
    except json.JSONDecodeError:
        return str(arguments)
    return args.get("command", json.dumps(args)) if isinstance(args, dict) else str(args)


def parse(traj):
    """Parse a loaded .traj.json dict into (steps, meta)."""
    if traj.get("trajectory_format") != "mini-swe-agent-1.1":
        raise ValueError(f"unsupported trajectory_format: {traj.get('trajectory_format')}")
    msgs = traj["messages"]
    if [m.get("role") for m in msgs[:2]] != ["system", "user"]:
        raise ValueError("expected system + task prompt as first two messages")

    steps, schemas = [], set()
    for m in msgs[2:]:
        role, mtype = m.get("role"), m.get("type")
        if role == "assistant":
            schemas.add("B" if "reasoning" in m else "A")
            reasoning = _as_text(m.get("reasoning_content") if "reasoning_content" in m else m.get("reasoning"))
            actions = [_command(tc["function"]["arguments"]) for tc in (m.get("tool_calls") or [])]
            steps.append(Step(text=_as_text(m.get("content")), reasoning=reasoning, actions=actions))
        elif m.get("object") == "response":
            schemas.add("C")
            step = Step()
            for item in m.get("output") or []:
                t = item.get("type")
                if t == "message":
                    step.text += _as_text(item.get("content"))
                elif t == "reasoning":
                    step.reasoning += "\n".join(s.get("text", "") for s in item.get("summary") or [])
                elif t == "function_call":
                    step.actions.append(_command(item.get("arguments")))
            steps.append(step)
        elif role == "tool" or mtype == "function_call_output":
            obs = _as_text(m.get("content")) if role == "tool" else _as_text(m.get("output"))
            if not steps:
                raise ValueError("observation before any agent step")
            steps[-1].observations.append(obs)
        elif role == "user":
            # Format-error feedback can arrive before any stored agent message (GLM-5, DeepSeek:
            # the malformed response itself is not logged). Keep it as a feedback-only step.
            if not steps or steps[-1].observations or steps[-1].feedback:
                steps.append(Step())
            steps[-1].feedback.append(_as_text(m.get("content")))
        elif role == "exit":
            pass
        else:
            raise ValueError(f"unrecognized message: role={role} type={mtype} keys={sorted(m)[:8]}")

    info = traj.get("info") or {}
    meta = {
        "schema": "".join(sorted(schemas)),
        "exit_status": info.get("exit_status"),
        "submission": info.get("submission") or "",
        "n_turns": sum(1 for m in msgs if m.get("role") == "assistant" or m.get("object") == "response"),
        "n_env_errors": sum(len(s.feedback) for s in steps),
        "n_actions": sum(len(s.actions) for s in steps),
        "agent_text_chars": sum(len(s.text) for s in steps),
        "reasoning_chars": sum(len(s.reasoning) for s in steps),
    }
    return steps, meta


def _middle_cut(text, max_tokens, enc):
    toks = encoder(enc).encode(text, disallowed_special=())
    if len(toks) <= max_tokens:
        return text, False
    head, tail = max_tokens // 2, max_tokens - max_tokens // 2
    dec = encoder(enc).decode
    return dec(toks[:head]) + f"\n[... {len(toks) - max_tokens} tokens omitted ...]\n" + dec(toks[-tail:]), True


_FEEDBACK_KINDS = [
    ("No tool calls found", "the response contained no tool call."),
    ("Error parsing tool call", "the tool call arguments could not be parsed."),
    ("Unknown tool", "the response named an unknown tool."),
]


def canonical_feedback(fb):
    """Collapse format-error feedback to a fixed sentence.

    The raw message echoes the malformed response (narration, `</think>` / `<tool_call>` tags
    that identify the model family) and appends boilerplate guidance; neither belongs in the
    judge's view. Unrecognized feedback is kept, capped at 1,000 characters.
    """
    if fb.startswith("Tool call error:"):
        for needle, sentence in _FEEDBACK_KINDS:
            if needle in fb:
                return "Tool call error: " + sentence
    return fb[:1000]


def render_step(i, step, agent_text, max_obs_tokens, enc):
    out, cut = [f"### Step {i}"], 0
    if agent_text:
        thought = "\n".join(x for x in (step.reasoning.strip(), step.text.strip()) if x)
        if thought:
            out.append(f"[AGENT]\n{thought}")
    for a in step.actions:
        out.append(f"[ACTION] $ {a}")
    for o in step.observations:
        o, was_cut = _middle_cut(o, max_obs_tokens, enc)
        cut += was_cut
        out.append(f"[OBSERVATION]\n{o}")
    for fb in step.feedback:
        out.append(f"[ENVIRONMENT] {canonical_feedback(fb)}")
    return "\n".join(out), cut


def render(steps, agent_text=False, max_tokens=100_000, max_obs_tokens=4_000, enc="o200k_base"):
    """Render steps to text; middle-truncate whole steps if over max_tokens.

    Returns (text, stats) with stats: tokens_untruncated, tokens, truncated, steps_omitted,
    obs_truncated, scrubbed.
    """
    raw_blocks = [render_step(i + 1, s, agent_text, 10**9, enc)[0] for i, s in enumerate(steps)]
    tokens_untruncated = n_tokens("\n\n".join(raw_blocks), enc)

    blocks, obs_cut = [], 0
    for i, s in enumerate(steps):
        b, c = render_step(i + 1, s, agent_text, max_obs_tokens, enc)
        blocks.append(b)
        obs_cut += c
    scrubbed = 0
    for i, b in enumerate(blocks):
        blocks[i], k = scrub(b)
        scrubbed += k
    sizes = [n_tokens(b, enc) + 1 for b in blocks]

    omitted = 0
    if sum(sizes) > max_tokens:
        # Keep whole steps from both ends, alternating, until the budget is spent.
        budget = max_tokens - 50
        lo, hi = 0, len(blocks) - 1
        keep_head, keep_tail, take_head = [], [], True
        while lo <= hi:
            idx = lo if take_head else hi
            if sizes[idx] > budget:
                break
            budget -= sizes[idx]
            if take_head:
                keep_head.append(idx)
                lo += 1
            else:
                keep_tail.append(idx)
                hi -= 1
            take_head = not take_head
        omitted = len(blocks) - len(keep_head) - len(keep_tail)
        marker = f"[... {omitted} steps omitted from the middle of the trajectory for length ...]"
        blocks = [blocks[i] for i in keep_head] + [marker] + [blocks[i] for i in reversed(keep_tail)]

    text = "\n\n".join(blocks)
    return text, {
        "tokens_untruncated": tokens_untruncated,
        "tokens": n_tokens(text, enc),
        "truncated": omitted > 0,
        "steps_omitted": omitted,
        "obs_truncated": obs_cut,
        "scrubbed": scrubbed,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path")
    ap.add_argument("--agent-text", action="store_true", help="include agent narration and reasoning")
    ap.add_argument("--max-tokens", type=int, default=100_000)
    ap.add_argument("--max-obs-tokens", type=int, default=4_000)
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    with open(args.path, encoding="utf-8") as f:
        steps, meta = parse(json.load(f))
    text, stats = render(steps, args.agent_text, args.max_tokens, args.max_obs_tokens)
    print(text)
    print("\n# meta", json.dumps({k: v for k, v in meta.items() if k != "submission"}), json.dumps(stats))


if __name__ == "__main__":
    main()

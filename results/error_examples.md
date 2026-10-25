# Curated judge errors (for the qualitative paragraph)

All are **unresolved** trajectories (the hidden tests failed) that judges called `success`.
Full candidate list: `results/error_candidates.md`. Rationales are quoted verbatim.

## Pattern 1: deferring to the agent's own verification

Among high-confidence (>= 0.9) false positives, **52%** of full-condition rationales cite the agent's own tests,
reproduction scripts or "existing tests pass", versus **1%** in the final-patch-only condition. This is a keyword
match, so treat it as approximate. The agent's self-checks do not include the hidden FAIL_TO_PASS tests, so
"the agent verified it" is weak evidence that judges treat as strong.

1. **astropy-14365** (GLM-5 agent). *All four judges* said success, confidence 0.9–1.0.
   DeepSeek (0.95): "The agent's own tests show that lowercase and mixed-case commands now parse successfully, and
   the existing test suite passes, indicating no regression."
   Gemma (1.0): "The agent verified the fix with reproduction tests and existing test suites."

2. **django-14140** (Gemini agent). All four judges wrong.
   Gemma (1.0): "The agent verified the fix with a reproduction script and existing tests, ensuring that both the
   crash is resolved and existing behavior for standard `Q` objects is preserved."

3. **django-14534** (Gemini agent). All four judges wrong.
   Gemma (1.0): "The patch passes the reproduction script and the relevant Django test suite."

## Pattern 2: explaining away negative evidence

4. **matplotlib-26291** (Kimi agent). DeepSeek, 0.95: "...a minimal, targeted fix that doesn't break existing
   functionality. **The agent's own tests failed due to unrelated environment issues (NumPy version mismatches),
   not the patch itself.**"

5. **sympy-12096** (GLM-5 agent). DeepSeek, 0.95: "...existing test suites pass **with only unrelated failures**."

## Pattern 3: the trajectory talks the judge out of a correct rejection

Across judges, adding the trajectory turned 91 correct rejections into false positives, while turning only 34 false
positives into correct rejections. (On resolved runs the shift is likewise toward success: 102 vs 37.)

6. **sympy-12481** (GPT-5.2 agent), DeepSeek judge.
   *Final patch only (failure, 0.7):* "...also incorrectly allows invalid cycles (with repeated elements within a
   cycle). This likely breaks existing tests that expect a ValueError..."
   *Full trajectory (success, 0.95):* "...still rejecting duplicates within a single cycle via the Cycle class..."

7. **sympy-12489** (DeepSeek agent), DeepSeek judge.
   *Final patch only (failure, 0.7):* "...it fails to update the branch in `__new__` that handles a `Permutation`
   (or subclass) argument when the size does not match..."
   *Full trajectory (success, 0.95):* "...The agent's comprehensive tests show..."

8. **django-11141** (DeepSeek agent), DeepSeek judge.
   *Final patch only (failure, 0.6):* flags that the agent's test change asserts the wrong behavior.
   *Full trajectory (success, 0.9):* "...The agent also updated the relevant test to reflect the new [behavior]."

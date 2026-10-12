---
name: run-evals
description: Benchmark agents with vs without Skills using evals/agents.toml and evals/evals.toml, then present the results table. Only when the user runs /run-evals.
disable-model-invocation: true
argument-hint: "[agent ...] e.g. gemma_26b, gemma_31b (default: all agents)"
allowed-tools: Bash(python3 *) Read
---

# /run-evals

Runs `${CLAUDE_SKILL_DIR}/run_evals.py`, which benchmarks each requested agent in two arms
(`+skills`, `-noskills`) on every eval in `evals/evals.toml`.

## Steps

1. If `evals/agents.toml` or `evals/evals.toml` is missing, run
   `python3 "${CLAUDE_SKILL_DIR}/run_evals.py" --init`, tell the user to fill them in, and stop.

2. Start the benchmark from the project root **in the background** (it can take far longer than
   the Bash tool's timeout):

   ```
   python3 "${CLAUDE_SKILL_DIR}/run_evals.py" $ARGUMENTS
   ```

   Pass the arguments through as given; the script accepts commas and/or spaces.
   If it exits immediately with an error (unknown agent, bad config), show the error and stop.

3. While it runs, check its output now and then. Each finished run prints one
   `[i/N] ...` line; don't relay every line, just give the user a short progress note occasionally.

4. When the output contains `RESULTS: <path>`, read that `results.md` and show the table to
   the user exactly as written, followed by the Notes section.

5. Then add at most three sentences: where the `+skills` arm clearly beat or lost to the
   `-noskills` arm for the same model, and any runs that errored. Don't speculate beyond
   the numbers. With 3 runs per arm, call differences under ~0.15 confidence or a single
   pass/fail flip "noise".

Don't re-run failed runs on your own; report them and let the user decide.

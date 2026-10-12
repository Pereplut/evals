# run-evals

Benchmarks Claude Code agents **with vs without your Skills**. It needs only Python 3.11+ and the `claude` CLI you already use.

## Install

1. Copy `.claude/skills/run-evals/` into your project.
2. From the project root, create the config templates:

   ```
   python3 .claude/skills/run-evals/run_evals.py --init
   ```

   This creates `evals/agents.toml`, `evals/evals.toml` and `evals/checks/`.
3. Edit both files. Model ids must be exactly what your gateway expects.
4. In Claude, run `/run-evals gemma_26b, gemma_31b`. You can also run the script directly in a terminal:

   ```
   python3 .claude/skills/run-evals/run_evals.py gemma_26b gemma_31b
   ```

Before the first real run, do a cheap smoke test:

```
python3 .claude/skills/run-evals/run_evals.py gemma_26b --runs 1 --evals explain_entrypoint --no-judge
```

## How the arms differ

| | `+skills` | `-noskills` |
|---|---|---|
| tools | Read, Glob, Grep, **Skill** | Read, Glob, Grep |
| flags | — | `--disable-slash-commands --disallowedTools Skill` |

Both arms set `CLAUDE_CODE_DISABLE_BUNDLED_SKILLS=1`, so only your own skills make a difference.

Agents are read-only and use `--permission-mode dontAsk`. A skill that needs Bash will be denied. If you want to allow it, add Bash to `defaults.tools`.

The `run-evals` skill itself has `disable-model-invocation: true`, so the `+skills` agents never see it.

## Output

The script writes everything to `evals/results/<timestamp>/`:

- `results.md`: the table, with one row per criterion × agent-arm × prompt, aggregated over runs.
- `results.jsonl`: one record per run, with the command, per-model token breakdown, skills invoked, every criterion value and each judge's score and reason. Judge calls are recorded separately and are **not** counted in agent tokens or duration.
- `outputs/eval_<N>_<agent>_r<run>.md`: each run's final answer. `N` is the eval's position in `evals.toml`.
- `transcripts/`: the full stream-json output of each run, for debugging.

## Columns

- **Duration** is `duration_ms` as reported by Claude Code, converted to seconds.
- **Tokens** are shown as `total->output`. Total is input plus cache read plus cache creation plus output, summed over every model in `modelUsage`. That includes subagents and internal calls.
- **Result** depends on the criterion type:
  - `regex`/`cmd` criteria show `true`, `false` or `k/n`.
  - `llm` criteria show the mean confidence. By default, every *other* model in `agents.toml` judges and the scores are averaged.

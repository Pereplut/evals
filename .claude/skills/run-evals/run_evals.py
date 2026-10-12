#!/usr/bin/env python3
"""run_evals.py - benchmark Claude Code agents WITH vs WITHOUT Skills.

Stdlib only (Python 3.11+). Every agent run is a headless `claude -p` call, so the
agents see your real .claude/skills/, your gateway and your token.

    python3 run_evals.py --init                  # create evals/agents.toml + evals/evals.toml
    python3 run_evals.py                         # all agents from agents.toml
    python3 run_evals.py gemma_26b, gemma_31b    # selected agents (commas/spaces both fine)
    python3 run_evals.py --report evals/results/2026-10-12_1015   # rebuild results.md

Each agent runs in two arms: `<name>+skills` and `<name>-noskills`.
Results land in evals/results/<timestamp>/{results.md, results.jsonl, outputs/, transcripts/}.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import time
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ARMS = (("skills", True), ("noskills", False))
NESTED_ENV_VARS = ("CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION")  # let us launch claude from inside claude

DEFAULTS = {
    "runs": 3,
    "timeout_s": 900,
    "claude_bin": "claude",
    "tools": ["Read", "Glob", "Grep"],  # agents are read-only; "Skill" is added in the +skills arm
    "extra_args": [],
    "judge": "panel",                   # "panel" | "none" | <agent name>
    "judge_tools": [],
    "judge_timeout_s": 300,
}


# --------------------------------------------------------------------------- config

@dataclass
class Agent:
    name: str
    model: str
    env: dict = field(default_factory=dict)
    extra_args: list = field(default_factory=list)


@dataclass
class Criterion:
    idx: int
    name: str
    check: str          # regex | cmd | llm
    spec: dict


@dataclass
class Eval:
    idx: int
    name: str
    prompt: str
    criteria: list[Criterion]


def load_toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def load_agents(path: Path) -> tuple[dict, dict[str, Agent]]:
    raw = load_toml(path)
    cfg = {**DEFAULTS, **raw.get("defaults", {})}
    cfg["env"] = raw.get("env", {})
    agents = {}
    for name, a in raw.get("agents", {}).items():
        if "model" not in a:
            sys.exit(f"agents.toml: agent '{name}' has no 'model'")
        agents[name] = Agent(name, a["model"], {**cfg["env"], **a.get("env", {})},
                             cfg["extra_args"] + a.get("extra_args", []))
    if not agents:
        sys.exit("agents.toml: no [agents.<name>] tables found")
    return cfg, agents


def load_evals(path: Path) -> list[Eval]:
    raw = load_toml(path)
    evals = []
    for i, e in enumerate(raw.get("eval", []), 1):
        crits = []
        for j, c in enumerate(e.get("criteria", []), 1):
            check = c.get("check", "llm")
            if check not in ("regex", "cmd", "llm"):
                sys.exit(f"evals.toml: eval '{e.get('name')}' criterion '{c.get('name')}': unknown check '{check}'")
            crits.append(Criterion(j, c.get("name", f"criterion_{j}"), check, c))
        if "prompt" not in e:
            sys.exit(f"evals.toml: eval #{i} has no 'prompt'")
        evals.append(Eval(i, e.get("name", f"eval_{i}"), e["prompt"].strip(), crits))
    if not evals:
        sys.exit("evals.toml: no [[eval]] entries found")
    return evals


# --------------------------------------------------------------------------- claude -p

def run_claude(cfg: dict, model: str, prompt: str, *, skills: bool, tools: list[str],
               cwd: Path, env_extra: dict, extra_args: list, timeout: int,
               allowed: list[str] | None = None, transcript: Path | None = None) -> dict:
    tool_list = list(tools) + (["Skill"] if skills else [])
    allow_list = (list(allowed) if allowed is not None else list(tools)) + (["Skill"] if skills else [])
    cmd = [cfg["claude_bin"], "-p", "--model", model,
           "--output-format", "stream-json", "--verbose",
           "--permission-mode", "dontAsk",          # anything that would prompt is denied
           "--tools", ",".join(tool_list)]
    if allow_list:
        cmd += ["--allowedTools", *allow_list]   # one argv per rule; anything else is denied (dontAsk)
    if not skills:
        cmd += ["--disable-slash-commands", "--disallowedTools", "Skill"]
    cmd += list(extra_args)

    env = os.environ.copy()
    for k in NESTED_ENV_VARS:
        env.pop(k, None)
    env["CLAUDE_CODE_DISABLE_BUNDLED_SKILLS"] = "1"   # compare only YOUR skills
    env.update({k: str(v) for k, v in env_extra.items()})

    t0 = time.monotonic()
    try:
        # prompt goes via stdin: variadic flags like --allowedTools would swallow a positional arg
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                           timeout=timeout, cwd=cwd, env=env)
        stdout, stderr, rc = p.stdout, p.stderr, p.returncode
    except subprocess.TimeoutExpired as e:
        stdout = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        stderr, rc = f"timeout after {timeout}s", -1
    except FileNotFoundError:
        sys.exit(f"claude binary not found: {cfg['claude_bin']!r} (set defaults.claude_bin in agents.toml)")
    wall = time.monotonic() - t0

    if transcript:
        transcript.write_text(stdout + (f"\n# STDERR\n{stderr}" if stderr else ""))

    res, skills_used = parse_stream(stdout)
    out = {"cmd": shlex.join(cmd), "wall_s": round(wall, 2), "returncode": rc,
           "skills_used": skills_used, "error": None}
    if res is None:
        out.update(text="", duration_s=round(wall, 2), tokens_total=0, tokens_output=0,
                   usage_detail={}, error=(stderr.strip() or "no result event in output")[-2000:])
        return out
    total, output, detail = count_tokens(res)
    out.update(text=res.get("result") or "",
               duration_s=round(res.get("duration_ms", wall * 1000) / 1000, 2),
               tokens_total=total, tokens_output=output, usage_detail=detail,
               num_turns=res.get("num_turns"), subtype=res.get("subtype"))
    if res.get("is_error") or rc != 0:
        out["error"] = (res.get("subtype") or "error") + (f": {stderr.strip()[-500:]}" if stderr.strip() else "")
    return out


def parse_stream(stdout: str) -> tuple[dict | None, list[str]]:
    """Return (final result event, names of skills the agent invoked)."""
    result, skills_used = None, []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "assistant":
            for c in (ev.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == "Skill":
                    inp = c.get("input") or {}
                    skills_used.append(str(inp.get("skill") or inp.get("command") or inp))
        if "duration_ms" in ev and (ev.get("type") == "result" or "modelUsage" in ev or "usage" in ev):
            result = ev
    return result, skills_used


def count_tokens(res: dict) -> tuple[int, int, dict]:
    """total = all input (incl. cache read/creation) + output, across every model call,
    subagents and internal calls included (that's what modelUsage covers)."""
    mu = res.get("modelUsage") or {}
    if mu:
        total = output = 0
        for m in mu.values():
            inp = m.get("inputTokens", 0) + m.get("cacheReadInputTokens", 0) + m.get("cacheCreationInputTokens", 0)
            output += m.get("outputTokens", 0)
            total += inp + m.get("outputTokens", 0)
        return total, output, {"modelUsage": mu}
    u = res.get("usage") or {}
    inp = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
    return inp + u.get("output_tokens", 0), u.get("output_tokens", 0), {"usage": u}


# --------------------------------------------------------------------------- checks

def check_regex(c: Criterion, text: str) -> bool:
    hit = re.search(c.spec["pattern"], text, re.MULTILINE) is not None
    return hit != bool(c.spec.get("negate", False))


def check_cmd(c: Criterion, output_file: Path, project: Path, cfg_dir: Path) -> tuple[bool, str]:
    cmdline = (c.spec["run"].replace("{output}", shlex.quote(str(output_file)))
                            .replace("{project}", shlex.quote(str(project)))
                            .replace("{evals}", shlex.quote(str(cfg_dir))))
    try:
        p = subprocess.run(cmdline, shell=True, cwd=project, capture_output=True, text=True,
                           timeout=int(c.spec.get("timeout_s", 120)))
        return p.returncode == 0, (p.stdout + p.stderr)[-1000:]
    except subprocess.TimeoutExpired:
        return False, "timeout"


JUDGE_PROMPT = """You are a strict, impartial evaluator. An AI agent was given this task:

<task>
{task}
</task>

This is the agent's final answer:

<answer>
{answer}
</answer>

For each criterion, give your confidence (0.0 = certainly NOT satisfied, 1.0 = certainly satisfied)
that the answer satisfies it.

Criteria:
{criteria}

Reply with ONLY this JSON, no prose, no code fences:
{{"scores": [{{"id": 1, "confidence": 0.0, "reason": "one short sentence"}}]}}"""


def judge_prompt(ev: Eval, crits: list[Criterion], answer: str) -> str:
    lines = []
    for c in crits:
        line = f"{c.idx}. {c.name}: {c.spec.get('rubric', c.name)}"
        if c.spec.get("reference"):
            line += f"\n   Reference answer: {c.spec['reference']}"
        lines.append(line)
    return JUDGE_PROMPT.format(task=ev.prompt, answer=answer or "(empty answer)", criteria="\n".join(lines))


def parse_judge(text: str, ids: list[int]) -> dict[int, dict]:
    scores: dict[int, dict] = {}
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            for s in json.loads(m.group(0)).get("scores", []):
                scores[int(s["id"])] = {"confidence": clamp(float(s["confidence"])), "reason": s.get("reason", "")}
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass
    for i in ids:  # lenient fallback for models that mangle JSON
        if i not in scores:
            m = re.search(rf'"id"\s*:\s*{i}\s*,\s*"confidence"\s*:\s*([0-9.]+)', text)
            if m:
                scores[i] = {"confidence": clamp(float(m.group(1))), "reason": "(parsed leniently)"}
    return scores


def clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


# --------------------------------------------------------------------------- formatting

def fmt_tokens(n: float) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return f"{n:.0f}"


def fmt_result(values: list) -> str:
    vals = [v for v in values if v is not None]
    if not vals:
        return "error"
    if all(isinstance(v, bool) for v in vals):
        passed = sum(vals)
        s = "true" if passed == len(vals) else "false" if passed == 0 else f"{passed}/{len(vals)}"
    else:
        s = f"{statistics.mean(float(v) for v in vals):.2f}"
    return s + (f" ({len(values) - len(vals)} err)" if len(vals) < len(values) else "")


def write_report(out_dir: Path) -> Path:
    runs = [json.loads(l) for l in (out_dir / "results.jsonl").read_text().splitlines() if l.strip()]
    runs = [r for r in runs if r.get("kind") == "run"]
    meta = json.loads((out_dir / "meta.json").read_text()) if (out_dir / "meta.json").exists() else {}

    groups: dict[tuple, list] = {}
    order: list[tuple] = []
    for r in sorted(runs, key=lambda r: (r["eval_idx"], r["agent"], r["arm"] != "skills", r["run"])):
        for c in r["criteria"]:
            key = (r["eval_idx"], c["idx"], c["name"], r["prompt_name"], r["agent_label"])
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append((r, c))
    order.sort(key=lambda k: (k[0], k[1]))  # stable: eval → criterion → agent/arm

    lines = [f"# Eval results — {meta.get('started', out_dir.name)}", "",
             f"Agents: {', '.join(meta.get('agents', []))} · runs per agent/arm: {meta.get('runs', '?')} · "
             f"judge: {meta.get('judge', '?')}", "",
             "Duration and tokens are means over runs. Tokens are `total->output`, where total is input "
             "(incl. cache) + output across every internal call. Result is a pass count for regex/cmd "
             "checks and the mean judge confidence for llm checks.", "",
             "| eval_criteria | agent_name | prompt_name | duration | tokens | result |",
             "|---|---|---|---|---|---|"]
    for key in order:
        _, _, cname, pname, label = key
        rows = groups[key]
        ok = [r for r, _ in rows if not r.get("error")]
        dur = f"{statistics.mean(r['duration_s'] for r in ok):.1f}" if ok else "—"
        tok = (f"{fmt_tokens(statistics.mean(r['tokens_total'] for r in ok))}->"
               f"{fmt_tokens(statistics.mean(r['tokens_output'] for r in ok))}") if ok else "—"
        res = fmt_result([None if r.get("error") else c.get("value") for r, c in rows])
        lines.append(f"| {cname} | {label} | {pname} | {dur} | {tok} | {res} |")

    # skill usage + errors: short, factual footnotes
    notes = []
    by_label: dict[str, list] = {}
    for r in runs:
        by_label.setdefault(r["agent_label"], []).append(r)
    for label, rs in sorted(by_label.items()):
        if label.endswith("+skills"):
            used = sum(1 for r in rs if r.get("skills_used"))
            names = sorted({s for r in rs for s in r.get("skills_used", [])})
            notes.append(f"- `{label}` invoked a skill in {used}/{len(rs)} runs"
                         + (f" ({', '.join(names)})" if names else ""))
        errs = [r for r in rs if r.get("error")]
        if errs:
            notes.append(f"- `{label}`: {len(errs)} failed run(s), e.g. {errs[0]['error'][:160]!r}")
    if notes:
        lines += ["", "## Notes", *notes]
    lines += ["", "Raw data: `results.jsonl` · agent answers: `outputs/` · full transcripts: `transcripts/`", ""]
    path = out_dir / "results.md"
    path.write_text("\n".join(lines))
    return path


# --------------------------------------------------------------------------- main flow

def log(msg: str) -> None:
    print(msg, flush=True)


def select_agents(args_agents: list[str], agents: dict[str, Agent]) -> list[Agent]:
    names = [n for a in args_agents for n in re.split(r"[,\s]+", a) if n]
    if not names:
        return list(agents.values())
    unknown = [n for n in names if n not in agents]
    if unknown:
        sys.exit(f"unknown agent(s): {', '.join(unknown)}. Defined in agents.toml: {', '.join(agents)}")
    return [agents[n] for n in dict.fromkeys(names)]


def judges_for(cfg: dict, all_agents: dict[str, Agent], producer: Agent) -> list[Agent]:
    mode = cfg["judge"]
    if mode == "none":
        return []
    if mode != "panel":
        if mode not in all_agents:
            sys.exit(f"defaults.judge = {mode!r} is not an agent in agents.toml")
        return [all_agents[mode]]
    # panel: every OTHER model judges (one judge per distinct model); fall back to self if alone
    seen, panel = {producer.model}, []
    for a in all_agents.values():
        if a.model not in seen:
            seen.add(a.model)
            panel.append(a)
    return panel or [producer]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("agents", nargs="*", help="agent names from agents.toml (default: all)")
    ap.add_argument("--config-dir", type=Path, default=Path("evals"), help="folder with agents.toml/evals.toml")
    ap.add_argument("--project", type=Path, default=Path.cwd(), help="folder the agents work in (default: cwd)")
    ap.add_argument("--runs", type=int, help="override defaults.runs")
    ap.add_argument("--evals", help="comma-separated eval names to run (default: all)")
    ap.add_argument("--no-judge", action="store_true", help="skip llm criteria")
    ap.add_argument("--init", action="store_true", help="create template config files and exit")
    ap.add_argument("--report", type=Path, help="rebuild results.md from an existing results folder")
    ap.add_argument("--dry-run", action="store_true", help="print the plan and the first command, run nothing")
    args = ap.parse_args()

    cfg_dir = args.config_dir.resolve()
    if args.init:
        tpl = HERE / "templates"
        for src in sorted(p for p in tpl.rglob("*") if p.is_file()):
            dst = cfg_dir / src.relative_to(tpl)
            if dst.exists():
                log(f"exists, kept: {dst}")
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(src, dst)
                log(f"created: {dst}")
        return
    if args.report:
        log(f"RESULTS: {write_report(args.report.resolve())}")
        return

    cfg, all_agents = load_agents(cfg_dir / "agents.toml")
    evals = load_evals(cfg_dir / "evals.toml")
    if args.evals:
        wanted = {n.strip() for n in args.evals.split(",")}
        evals = [e for e in evals if e.name in wanted] or sys.exit(f"no eval matches {wanted}")
    selected = select_agents(args.agents, all_agents)
    runs = args.runs or int(cfg["runs"])
    if args.no_judge:
        cfg["judge"] = "none"
    project = args.project.resolve()

    plan = [(n, ev, ag, arm, skills) for n in range(1, runs + 1) for ev in evals
            for ag in selected for arm, skills in ARMS]   # interleaved so drift hits all arms equally
    log(f"{len(plan)} agent runs: {len(selected)} agent(s) × 2 arms × {len(evals)} eval(s) × {runs} run(s); "
        f"judge={cfg['judge']}; project={project}")
    if args.dry_run:
        n, ev, ag, arm, skills = plan[0]
        log("first command (prompt is sent on stdin):")
        sk = ["Skill"] if skills else []
        tl, al = cfg["tools"] + sk, (cfg.get("allowed_tools") or cfg["tools"]) + sk
        log(f"  {cfg['claude_bin']} -p --model {ag.model} --output-format stream-json --verbose "
            f"--permission-mode dontAsk --tools {','.join(tl)} --allowedTools {shlex.join(al)} {' '.join(ag.extra_args)}")
        return

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out_dir = cfg_dir / "results" / stamp
    (out_dir / "outputs").mkdir(parents=True)
    (out_dir / "transcripts").mkdir()
    (out_dir / "meta.json").write_text(json.dumps({
        "started": datetime.now().isoformat(timespec="seconds"), "agents": [a.name for a in selected],
        "models": {a.name: a.model for a in selected}, "runs": runs, "judge": cfg["judge"],
        "project": str(project), "evals": [e.name for e in evals]}, indent=2))
    partial = open(out_dir / "runs.partial.jsonl", "a")

    # ---- phase 1: agent runs + deterministic checks
    records = []
    for i, (n, ev, ag, arm, skills) in enumerate(plan, 1):
        label = f"{ag.name}+skills" if skills else f"{ag.name}-noskills"
        stem = f"eval_{ev.idx}_{label}_r{n}"
        r = run_claude(cfg, ag.model, ev.prompt, skills=skills, tools=cfg["tools"], cwd=project,
                       env_extra=ag.env, extra_args=ag.extra_args, timeout=int(cfg["timeout_s"]),
                       allowed=cfg.get("allowed_tools"), transcript=out_dir / "transcripts" / f"{stem}.jsonl")
        output_file = out_dir / "outputs" / f"{stem}.md"
        output_file.write_text(r["text"])
        crits = []
        for c in ev.criteria:
            cr = {"idx": c.idx, "name": c.name, "check": c.check, "value": None, "detail": None}
            if not r["error"]:
                if c.check == "regex":
                    cr["value"] = check_regex(c, r["text"])
                elif c.check == "cmd":
                    cr["value"], cr["detail"] = check_cmd(c, output_file, project, cfg_dir)
            crits.append(cr)
        rec = {"kind": "run", "run": n, "eval_idx": ev.idx, "prompt_name": ev.name, "agent": ag.name,
               "model": ag.model, "arm": arm, "agent_label": label, "output_file": str(output_file.relative_to(out_dir)),
               **{k: v for k, v in r.items() if k != "text"}, "criteria": crits}
        records.append(rec)
        partial.write(json.dumps(rec) + "\n"); partial.flush()
        status = "ERROR " + r["error"][:80] if r["error"] else \
            f"{r['duration_s']:.1f}s {fmt_tokens(r['tokens_total'])}->{fmt_tokens(r['tokens_output'])}" + \
            (f" skills={r['skills_used']}" if r["skills_used"] else "")
        log(f"[{i}/{len(plan)}] {label} · {ev.name} · run {n}: {status}")

    # ---- phase 2: llm judging, grouped by judge model to avoid swapping models on the GPU
    judge_log = []
    if cfg["judge"] != "none":
        evals_by_idx = {e.idx: e for e in evals}
        jobs: dict[str, list] = {}  # judge agent name -> [(record, judge agent)]
        for rec in records:
            ev = evals_by_idx[rec["eval_idx"]]
            if rec["error"] or not any(c.check == "llm" for c in ev.criteria):
                continue
            for j in judges_for(cfg, all_agents, all_agents[rec["agent"]]):
                jobs.setdefault(j.name, []).append((rec, j))
        total_jobs = sum(len(v) for v in jobs.values())
        done = 0
        for jname, items in jobs.items():
            for rec, j in items:
                done += 1
                ev = evals_by_idx[rec["eval_idx"]]
                llm_crits = [c for c in ev.criteria if c.check == "llm"]
                answer = (out_dir / rec["output_file"]).read_text()
                jr = run_claude(cfg, j.model, judge_prompt(ev, llm_crits, answer), skills=False,
                                tools=cfg["judge_tools"], cwd=project, env_extra=j.env,
                                extra_args=j.extra_args, timeout=int(cfg["judge_timeout_s"]))
                scores = {} if jr["error"] else parse_judge(jr["text"], [c.idx for c in llm_crits])
                for c in rec["criteria"]:
                    if c["check"] == "llm":
                        c.setdefault("judges", {})[j.name] = scores.get(c["idx"])
                judge_log.append({"kind": "judge", "judge": j.name, "model": j.model, "for": rec["agent_label"],
                                  "eval": rec["prompt_name"], "run": rec["run"], "duration_s": jr["duration_s"],
                                  "tokens_total": jr["tokens_total"], "tokens_output": jr["tokens_output"],
                                  "error": jr["error"], "raw": jr["text"][-2000:]})
                log(f"[judge {done}/{total_jobs}] {jname} → {rec['agent_label']} · {rec['prompt_name']} · run {rec['run']}"
                    + (f": ERROR {jr['error'][:80]}" if jr["error"] else f": {len(scores)}/{len(llm_crits)} scored"))
        for rec in records:
            for c in rec["criteria"]:
                if c["check"] == "llm" and c.get("judges"):
                    confs = [s["confidence"] for s in c["judges"].values() if s]
                    c["value"] = round(statistics.mean(confs), 3) if confs else None

    with open(out_dir / "results.jsonl", "w") as f:
        for rec in records + judge_log:
            f.write(json.dumps(rec) + "\n")
    partial.close()
    (out_dir / "runs.partial.jsonl").unlink()
    log(f"RESULTS: {write_report(out_dir)}")


if __name__ == "__main__":
    main()

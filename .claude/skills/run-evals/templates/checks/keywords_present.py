"""cmd check: pass (exit 0) if every non-comment line of GOLDEN appears in ANSWER.

    python3 keywords_present.py ANSWER GOLDEN

Matching ignores case and collapses whitespace, so keep golden lines short and distinctive
(a register name, a value, a few key words), not whole sentences the model may rephrase.
A line like  `0x2A | TEMP_LIMIT`  passes if ANY of its |-separated alternatives is found.
"""
import re
import sys
from pathlib import Path


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


answer = norm(Path(sys.argv[1]).read_text())
golden = [l.strip() for l in Path(sys.argv[2]).read_text().splitlines()
          if l.strip() and not l.lstrip().startswith("#")]
if not golden:
    sys.exit(f"golden file has no entries yet: {sys.argv[2]}")

missing = [g for g in golden if not any(norm(alt) in answer for alt in g.split("|"))]
for g in missing:
    print(f"missing: {g}")
print(f"{len(golden) - len(missing)}/{len(golden)} present")
sys.exit(1 if missing else 0)

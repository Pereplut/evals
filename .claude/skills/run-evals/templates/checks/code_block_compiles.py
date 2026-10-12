"""Example `cmd` check: exit 0 if every ```python block in the answer file compiles."""
import re
import sys
from pathlib import Path

blocks = re.findall(r"```python\n(.*?)```", Path(sys.argv[1]).read_text(), re.DOTALL)
if not blocks:
    sys.exit("no python block")
for b in blocks:
    compile(b, "<answer>", "exec")  # raises SyntaxError -> non-zero exit

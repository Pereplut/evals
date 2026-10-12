---
name: read-spec
description: Read and search PDF documents (device specifications, datasheets, manuals). Use whenever a task needs information from a .pdf file, e.g. its version, revision or change history, register values, or requirements.
allowed-tools: Bash(python3 *) Read Grep Glob
---

# Reading PDF specs

PDFs can't be grepped directly. Convert them to text first, then search the text.

1. Convert the PDF:
   ```
   python3 "${CLAUDE_SKILL_DIR}/scripts/spec.py" path/to/file.pdf
   ```
   This writes `path/to/file.txt` next to the PDF, with `===== page N =====` separators, and prints its path.
   If the .txt is already up to date, the script says so; just use it.

2. Search the .txt with Grep instead of reading it whole. Use case-insensitive search and keep a few lines of context:
   - **Version / revision:** grep `version|revision|rev\.` on page 1 (title page) and in the header and footer lines.
     When several versions appear, the document's own version is the **latest** entry in the change
     history, which should match the title page. Older versions listed in the history are not it.
   - **Change history:** grep `change history|revision history|document history`, then Read that page range.
     History tables often wrap one entry across several lines: an entry continues until the next version
     number in the version column.

3. Answer from the text only. Quote the exact wording for versions and changes, and cite the page
   (`p. N`). If the text is garbled or empty, the PDF is probably scanned; say so rather than guessing.

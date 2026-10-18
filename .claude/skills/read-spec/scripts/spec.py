#!/usr/bin/env python3
"""spec.py - convert a PDF into a searchable .txt file.

    python3 spec.py path/to/device_spec.pdf            # -> path/to/device_spec.txt
    python3 spec.py device_spec.pdf -o /tmp/spec.txt
    python3 spec.py device_spec.pdf --force            # re-convert even if the .txt is up to date

Pages are separated by lines like `===== page 3 =====`, so grep -n hits tell you the page.
Backends, best first: pdftotext (poppler) -> pypdf -> pdfminer.six -> built-in stdlib fallback.
The fallback needs no installs but only handles simple, non-CID fonts.
"""
from __future__ import annotations

import argparse
import base64
import re
import shutil
import subprocess
import sys
import zlib
from pathlib import Path


def via_pdftotext(pdf: Path) -> list[str]:
    if not shutil.which("pdftotext"):
        raise ImportError("pdftotext not on PATH")
    out = subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", str(pdf), "-"],
                         capture_output=True, text=True, check=True).stdout
    return out.rstrip("\f").split("\f")


def via_pypdf(pdf: Path) -> list[str]:
    from pypdf import PdfReader
    return [p.extract_text() or "" for p in PdfReader(str(pdf)).pages]


def via_pdfminer(pdf: Path) -> list[str]:
    from pdfminer.high_level import extract_text
    return extract_text(str(pdf)).split("\f")


# ---- stdlib fallback: inflate content streams, pull text out of Tj/TJ operators

_STREAM = re.compile(rb"(?<=[>\s])stream\r?\n(.*?)\s*endstream", re.DOTALL)
_TEXT_OP = re.compile(rb"\[(.*?)\]\s*TJ|\((.*?)(?<!\\)\)\s*(?:Tj|'|\")|(T\*|Td|TD|Tm|ET)", re.DOTALL)
_ESC = {b"n": b"\n", b"r": b"", b"t": b"\t", b"b": b"", b"f": b"", b"(": b"(", b")": b")", b"\\": b"\\"}


def _unescape(s: bytes) -> str:
    s = re.sub(rb"\\([0-7]{1,3})", lambda m: bytes([int(m.group(1), 8) & 0xFF]), s)
    s = re.sub(rb"\\(.)", lambda m: _ESC.get(m.group(1), m.group(1)), s)
    return s.decode("latin-1")


def via_stdlib(pdf: Path) -> list[str]:
    chunks = []
    data = pdf.read_bytes()
    for sm in _STREAM.finditer(data):
        head = data[max(0, sm.start() - 1000):sm.start()]
        head, body = head[head.rfind(b" obj"):], sm.group(1)  # dict of THIS object only
        m = re.search(rb"/Filter\s*(\[[^\]]*\]|/\w+)", head)
        filters = re.findall(rb"/(\w+)", m.group(1)) if m else []
        try:
            for f in filters:  # applied in the order listed
                if f in (b"ASCII85Decode", b"A85"):
                    body = base64.a85decode(body.strip().removeprefix(b"<~").removesuffix(b"~>"))
                elif f in (b"FlateDecode", b"Fl"):
                    # decompressobj: _STREAM trims whitespace before `endstream`, which can cut the
                    # trailing checksum; the data itself still decodes without it
                    body = zlib.decompressobj().decompress(body)
                else:
                    raise ValueError(f)  # images / unsupported filters
        except (ValueError, zlib.error):
            continue
        if b"BT" not in body:
            continue
        text = []
        for arr, single, op in _TEXT_OP.findall(body):
            if op:
                text.append("\n" if op in (b"T*", b"ET") else " ")
            elif single:
                text.append(_unescape(single))
            else:
                text.extend(_unescape(s) for s in re.findall(rb"\((.*?)(?<!\\)\)", arr, re.DOTALL))
        chunk = re.sub(r"[ \t]+", " ", "".join(text))
        chunk = re.sub(r" ?\n ?", "\n", chunk).strip()
        chunk = re.sub(r"\n{3,}", "\n\n", chunk)
        if chunk:
            chunks.append(chunk)
    if not chunks:
        raise ValueError("no extractable text (scanned PDF or CID fonts?)")
    return chunks  # one chunk per content stream ~ one per page for most PDFs


BACKENDS = (("pdftotext", via_pdftotext), ("pypdf", via_pypdf),
            ("pdfminer", via_pdfminer), ("stdlib-fallback", via_stdlib))


def convert(pdf: Path) -> tuple[list[str], str]:
    errors = []
    for name, fn in BACKENDS:
        try:
            pages = fn(pdf)
            if any(p.strip() for p in pages):
                return pages, name
            errors.append(f"{name}: empty text")
        except (ImportError, ValueError, subprocess.CalledProcessError) as e:
            errors.append(f"{name}: {e}")
        except Exception as e:  # a broken backend must not stop the chain
            errors.append(f"{name}: {type(e).__name__}: {e}")
    sys.exit("could not extract text:\n  " + "\n  ".join(errors))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf", type=Path)
    ap.add_argument("-o", "--output", type=Path, help="default: same path with .txt")
    ap.add_argument("--force", action="store_true", help="re-convert even if the .txt is newer than the PDF")
    a = ap.parse_args()

    if not a.pdf.is_file():
        sys.exit(f"not a file: {a.pdf}")
    out = a.output or a.pdf.with_suffix(".txt")
    if out.exists() and not a.force and out.stat().st_mtime >= a.pdf.stat().st_mtime:
        print(f"up to date: {out}")
        return

    pages, backend = convert(a.pdf)
    body = "\n".join(f"===== page {i} =====\n{p.rstrip()}\n" for i, p in enumerate(pages, 1))
    out.write_text(body, encoding="utf-8")
    print(f"wrote {out} ({len(pages)} pages, {len(body):,} chars, backend={backend})")


if __name__ == "__main__":
    main()

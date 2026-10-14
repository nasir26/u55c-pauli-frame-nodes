#!/usr/bin/env python3
"""Fails if prose contains dashes used as punctuation (revision rule 4).

Checks .tex (prose, captions, table text; math, URLs, DOIs, labels, refs, citations, comments and BibTeX files are
skipped), .md files and, with --git <repo>, the commit messages of that repository.
Flags: em dash, en dash, LaTeX --- and -- outside math and numbers in BibTeX, and a spaced hyphen " - ".
Hyphenated compounds (multi-FPGA, double-buffered) are allowed.
usage: lint_dashes.py FILE... [--git REPO]
"""
import re, subprocess, sys

PAT = [(re.compile("\u2014"), "em dash"), (re.compile("\u2013"), "en dash"), (re.compile(r"(?<!-)---(?!-)"), "LaTeX ---"),
       (re.compile(r"(?<![-!<])--(?![->])"), "LaTeX --"), (re.compile(r"(?<=\w) - (?=\w)"), "spaced hyphen")]


def strip_tex(line):
    line = re.sub(r"(?<!\\)%.*", "", line)                          # comments
    line = re.sub(r"\$\$.*?\$\$|\$.*?\$|\\\(.*?\\\)|\\\[.*?\\\]", " ", line)   # inline math
    line = re.sub(r"\\(url|href|doi|label|ref|eqref|cite\w*|includegraphics|input|bibliography\w*)(\[[^]]*\])?\{[^}]*\}", " ", line)
    return line


def lint_tex(path):
    bad = []
    math_env = re.compile(r"\\begin\{(equation|align|gather|multline|eqnarray|displaymath|math)\*?\}")
    end_env = re.compile(r"\\end\{(equation|align|gather|multline|eqnarray|displaymath|math)\*?\}")
    inmath = False
    for i, line in enumerate(open(path, encoding="utf8"), 1):
        if math_env.search(line):
            inmath = True
        if inmath:
            if end_env.search(line):
                inmath = False
            continue
        s = strip_tex(line)
        for p, what in PAT:
            if p.search(s):
                bad.append((path, i, what, line.rstrip()))
    return bad


def lint_text(path, text=None, label=None):
    bad = []
    text = open(path, encoding="utf8").read() if text is None else text
    incode = False
    for i, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith("```"):
            incode = not incode
            continue
        if incode or line.lstrip().startswith("|---") or re.match(r"^\s*[-*] ", line) and not re.search(r"\w - \w|\u2014|\u2013", line):
            continue
        s = re.sub(r"`[^`]*`|https?://\S+|doi:\S+", " ", line)
        for p, what in PAT:
            if what.startswith("LaTeX"):
                continue
            if p.search(s):
                bad.append((label or path, i, what, line.rstrip()))
    return bad


def main():
    args = sys.argv[1:]
    bad = []
    if "--git" in args:
        k = args.index("--git"); repo = args[k + 1]; args = args[:k] + args[k + 2:]
        log = subprocess.run(["git", "-C", repo, "log", "--format=%H%n%B%n@@END@@"], capture_output=True, text=True).stdout
        for block in log.split("@@END@@"):
            lines = block.strip().splitlines()
            if lines:
                bad += lint_text(None, "\n".join(lines[1:]), label=f"commit {lines[0][:8]}")
    for f in args:
        if f.endswith(".bib"):
            continue
        bad += lint_tex(f) if f.endswith(".tex") else lint_text(f)
    for b in bad:
        print(f"{b[0]}:{b[1]}: {b[2]}: {b[3][:160]}")
    print(f"lint_dashes: {len(bad)} problem(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

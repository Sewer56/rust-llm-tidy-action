#!/usr/bin/env python3
"""Render the action's "not tidy" comment table from rust-llm-tidy JSON output.

Reads the JSON array of lint findings that `rust-llm-tidy --output-mode json`
writes to stdout (the file path is argv[1]) and prints the Markdown table
(`| File | Line | Severity | Code | Reason |`) to stdout. Prints nothing when
the file is missing, unparseable, or contains no findings, so the caller falls
back to its plain file list.

Table-cell escaping mirrors the previous sed-based rendering: the reason cell
(derived from unconstrained source text) has its pipes turned into `&#124;`
entities so it cannot split a table cell or inject content into the PR comment
body; file, severity, and code cells are constrained fields rendered as-is
(file and code wrapped in the caller's inline code markup).
"""
import json
import sys


def main(json_path):
    try:
        with open(json_path, encoding="utf-8") as fh:
            findings = json.load(fh)
    except (OSError, ValueError):
        # Missing or invalid JSON means nothing to render; caller uses fallback.
        return

    if not findings:
        return

    print("| File | Line | Severity | Code | Reason |")
    print("| ---- | ---- | -------- | ---- | ------ |")
    for d in findings:
        path = d.get("path", "")
        line = d.get("line", "")
        severity = d.get("severity", "")
        code = d.get("code", "")
        reason = str(d.get("message", "")).replace("|", "&#124;")
        print(f"| `{path}` | {line} | {severity} | `{code}` | {reason} |")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: json_table.py <rlt-run.json>", file=sys.stderr)
        sys.exit(2)
    main(sys.argv[1])

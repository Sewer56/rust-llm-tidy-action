#!/usr/bin/env python3
"""Render the action's PR comment table/block from rust-llm-tidy JSON output.

Reads the JSON array of records that `rust-llm-tidy --output-mode json` writes
to stdout (the file path is argv[1]) and prints Markdown to stdout. It reads
the unified base fields `path, line, severity, code, message`; `item_kind` and
`item_name` are part of the input schema (kept for compatibility) but are not
rendered, and operation-specific extras are ignored so an op can add richer
fields without breaking this renderer. Records are split by severity:
`error`/`warning` lint findings render into the lint table
(`| File | Line | Severity | Code | Reason |`), while `severity: "success"`
change records (dry-run edits reorder/fix/vis would make) render into a
"Changes" block alongside it. Prints nothing when the file is missing,
unparseable, or contains no records, so the caller falls back to its plain
file list.

Table-cell escaping mirrors the previous sed-based rendering: cells derived
from unconstrained source text (the reason/change message) have their pipes
turned into `&#124;` entities so they cannot split a table cell or inject
content into the PR comment body; file, severity, and code cells are
constrained fields rendered as-is (file and code wrapped in inline code
markup).
"""
import json
import sys


def fmt_line(raw):
    """Column text for a record's line.

    Records that carry a line render it as-is; records with no specific line
    (an absent, `null`, or empty value - the CLI serializes a missing line as
    `null`, e.g. for link and table fixes) render `-` so the reader can tell
    "no line" from line 1..n. A numeric `0` is also tolerated (older binaries
    emitted it as the no-line sentinel).
    """
    return "-" if not raw else raw


def main(json_path):
    try:
        with open(json_path, encoding="utf-8") as fh:
            records = json.load(fh)
    except (OSError, ValueError):
        # Missing or invalid JSON means nothing to render; caller uses fallback.
        return

    if not records:
        return

    lints = [d for d in records if d.get("severity") in ("error", "warning")]
    changes = [d for d in records if d.get("severity") == "success"]

    rows = []

    if lints:
        rows.append("| File | Line | Severity | Code | Reason |")
        rows.append("| ---- | ---- | -------- | ---- | ------ |")
        for d in lints:
            path = d.get("path", "")
            line = fmt_line(d.get("line"))
            severity = d.get("severity", "")
            code = d.get("code", "")
            reason = str(d.get("message", "")).replace("|", "&#124;")
            rows.append(f"| `{path}` | {line} | {severity} | `{code}` | {reason} |")

    if changes:
        rows.append("### Changes")
        rows.append("| File | Line | Code | Change |")
        rows.append("| ---- | ---- | ---- | ------ |")
        for d in changes:
            path = d.get("path", "")
            line = fmt_line(d.get("line"))
            code = d.get("code", "")
            change = str(d.get("message", "")).replace("|", "&#124;")
            rows.append(f"| `{path}` | {line} | `{code}` | {change} |")

    if rows:
        print("\n".join(rows))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: json_table.py <rlt-run.json>", file=sys.stderr)
        sys.exit(2)
    main(sys.argv[1])

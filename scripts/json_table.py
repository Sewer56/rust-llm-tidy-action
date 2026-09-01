#!/usr/bin/env python3
"""Render the action's PR comment from rust-llm-tidy JSON output.

Reads the JSON array of records that `rust-llm-tidy --output-mode json`
writes to stdout (the file path is argv[1]) and prints Markdown to stdout.

- Lint findings (`severity` `error`/`warning`) render as finding entries
  grouped by severity, errors first: one bullet per finding with the code,
  a short title, a `path:line` location, and the message. Sentences after
  the first in a message render as sub-bullets, so multi-sentence guidance
  (DOC007, DOC008) stays readable.
- Change records (`severity: "success"`) render as a "Changes" table.

Locations link to the immutable blob at the commit the run linted when
`RLT_BLOB_BASE` is set to an `.../blob/<sha>/` URL prefix (the action
passes the PR head SHA); without it they render as plain code text. Link
text and destination are escaped so an untrusted repo path cannot inject
markdown into the comment body. Change records without a line (table and
link fixes) show `-` in the Changes table.

Change-table cells derived from unconstrained source text (the change
message) have their pipes turned into `&#124;` entities so they cannot
split a table cell or inject content into the PR comment body; other
fields are rendered as-is. Prints nothing when the file is missing,
unparseable, or contains no records, so the caller falls back to its
plain file list.
"""
import json
import os
import re
import sys
from urllib.parse import quote

# Compatibility fallback: short human titles per lint code, used only when a
# record carries no `title` of its own. Newer rust-llm-tidy binaries emit a
# friendly `title` on every lint record, but this action's default
# `binary-source: prebuilt` mode runs released binaries that predate the
# field and never emit it. finding_lines resolves the bullet title as the
# record's `title`, then this map, then the raw code; the map is retained
# because the action does not enforce a minimum binary version.
TITLES = {
    "DOC001": "missing documentation",
    "DOC002": "missing `# Errors` section",
    "DOC003": "vague `# Errors` section",
    "DOC004": "missing `# Arguments` section",
    "DOC005": "undocumented parameter",
    "DOC006": "placeholder text",
    "DOC007": "oversized paragraph",
    "DOC008": "long line",
    "TEST001": "non-behavioral test name",
}


def fmt_line(raw):
    """Column text for a change record's line.

    Records that carry a line render it as-is; records with no specific
    line (an absent, `null`, or empty value - the CLI serializes a missing
    line as `null`, e.g. for link and table fixes) render `-` so the
    reader can tell "no line" from line 1..n. A numeric `0` is also
    tolerated (older binaries emitted it as the no-line sentinel).
    """
    return "-" if not raw else raw


def escape_link_text(text):
    """Make text safe as markdown link text.

    Repo file paths are attacker-controlled (PR authors name files), so
    `]`/`[`/`\\` and control characters (newline smuggles line structure)
    are removed or escaped before the path enters the `[...]` link span;
    otherwise a crafted path injects markdown into the bot comment.
    """
    text = "".join(ch for ch in text if ch >= " ")
    return text.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def location(path, line):
    """Markdown `path:line` location, linked when `RLT_BLOB_BASE` is set.

    The link targets `<RLT_BLOB_BASE><path>#L<line>`; callers pass a
    `/blob/<sha>/` prefix so the link stays pinned to the commit the run
    linted and survives later pushes. Link text is escaped and the
    destination angle-bracketed so an untrusted path cannot break out of
    either. Without the prefix the location renders as plain code text.
    """
    text = f"{path}:{line}" if line else path
    base = os.environ.get("RLT_BLOB_BASE", "").rstrip("/")
    if not base:
        return f"`{text}`"
    url = f"{base}/{quote(path)}"
    if line:
        url += f"#L{line}"
    return f"[{escape_link_text(text)}](<{url}>)"


def split_guidance(message):
    """Split a finding message into its summary sentence and guidance.

    The split happens after sentence (`. `) and clause (`; `) endings, so
    DOC007/DOC008 guidance renders as one sub-bullet per instruction.
    Single-sentence messages keep their single line.
    """
    parts = [p for p in re.split(r"(?<=[.;])[ \t]+", message) if p]
    if len(parts) < 2:
        return message, []
    return parts[0], parts[1:]


def finding_lines(record):
    """Markdown lines for one lint finding: bullet, summary, sub-bullets."""
    code = record.get("code", "")
    # Record title first; missing/null/empty falls through to the map, and
    # an unknown code falls through to the raw code (never renders empty).
    title = record.get("title") or TITLES.get(code) or code
    path = record.get("path", "")

    # DOC007/DOC008 messages repeat the location as a `path: ` prefix;
    # drop it, the bullet already shows the location.
    message = str(record.get("message", "")).replace("\n", " ").strip()
    prefix = f"{path}: "
    if message.startswith(prefix):
        message = message[len(prefix):]
    name = record.get("item_name")
    if name:
        message += f" ({record.get('item_kind', '')} `{name}`)"

    summary, guidance = split_guidance(message)
    lines = [f"- **`{code}` {title}** - {location(path, record.get('line'))}"]
    lines.append(f"  {summary}")
    lines.extend(f"  - {part}" for part in guidance)
    return lines


def counts_line(errors, warnings, changes):
    """`N errors, M warnings, K changes.` over the non-zero groups only."""
    parts = []
    for count, noun in ((errors, "error"), (warnings, "warning"),
                        (changes, "change")):
        if count:
            parts.append(f"{count} {noun}" + ("" if count == 1 else "s"))
    return ", ".join(parts) + "."


def main(json_path):
    try:
        with open(json_path, encoding="utf-8") as fh:
            records = json.load(fh)
    except (OSError, ValueError):
        # Missing or invalid JSON means nothing to render; caller uses fallback.
        return

    if not records:
        return

    errors = [d for d in records if d.get("severity") == "error"]
    warnings = [d for d in records if d.get("severity") == "warning"]
    changes = [d for d in records if d.get("severity") == "success"]

    out = [counts_line(len(errors), len(warnings), len(changes))]
    for header, group in (("Errors", errors), ("Warnings", warnings)):
        if not group:
            continue
        out.append("")
        out.append(f"### {header}")
        for record in group:
            out.append("")
            out.extend(finding_lines(record))

    if changes:
        out.append("")
        out.append("### Changes")
        out.append("| File | Line | Code | Change |")
        out.append("| ---- | ---- | ---- | ------ |")
        for d in changes:
            path = d.get("path", "")
            line = fmt_line(d.get("line"))
            code = d.get("code", "")
            change = str(d.get("message", "")).replace("|", "&#124;")
            out.append(f"| `{path}` | {line} | `{code}` | {change} |")

    print("\n".join(out))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: json_table.py <rlt-run.json>", file=sys.stderr)
        sys.exit(2)
    main(sys.argv[1])

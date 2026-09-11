#!/usr/bin/env python3
"""Render the action's PR comment from rust-llm-tidy JSON output.

Reads the JSON array of records that `rust-llm-tidy --output-mode json`
writes (the file path is the argument) and renders Markdown:
`render` returns the text for callers, `main` prints it.

- Findings: group errors, warnings, hints, and reminders separately.
- Bullets: show each finding's code, title, `path:line` location, and message.
  The code links to the docs lints table (`LINT_CODES_URL`).
- Guidance: split later message sentences into sub-bullets for readability.
- Hints: show suggestions to investigate in a separate trailing section.
- Reminders: note their default changed-line scope in a separate section.
- AI reminders: collapse their section in a `<details>` block, so a human
  reading the report sees the heading without the AI-only guidance.
- Change records (`severity: "success"`) render as a "Changes" table.

Location links use an `.../blob/<sha>/` URL prefix to identify an exact commit:

- Explicit `base`: used by the sticky report for each finding's original commit.
- `RLT_BLOB_BASE`: environment fallback, set by the action to the PR head SHA.
- No base: render the location as plain code text.

Link text and destination are escaped so an untrusted repo path cannot
inject markdown into the comment body. Change records without a line
(table and link fixes) show `-` in the Changes table.

Change-table cells derived from unconstrained source text (the change
message) have their pipes turned into `&#124;` entities so they cannot
split a table cell or inject content into the PR comment body; other
fields are rendered as-is.

Prints nothing when the file is missing, unparseable, or contains no
records. The caller then falls back to its plain file list.
"""
import json
import os
import re
from urllib.parse import quote

LINT_CODES_URL = (
    "https://github.com/Sewer56/rust-llm-tidy/blob/main/docs/lints.md#codes"
)

FINDING_SECTIONS = (
    ("error", "Errors"),
    ("warning", "Warnings"),
    ("hint", "Hints - consider looking at these"),
    ("reminder", "Reminders - changed lines by default"),
    ("ai_reminder", "Reminders for AI Language Models"),
)

# Findings of this severity are guidance for AI agents, not for a human
# skimming the report, so their section renders collapsed.
COLLAPSED_SEVERITY = "ai_reminder"


def section_title(severity):
    """Configured section title for `severity`."""
    return next(header for code, header in FINDING_SECTIONS if code == severity)


def fmt_line(raw):
    """Column text for a change record's line.

    Missing, null, empty, or zero values render as `-`; other values stay as-is.
    The CLI uses null for fixes without a line, such as link and table fixes.
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


def location(path, line, base=None):
    """Markdown `path:line` location, linked when a blob base is given.

    The link targets `<base>/<path>#L<line>`; callers pass a
    `/blob/<sha>/` prefix so the link stays pinned to the commit the run
    linted and survives later pushes.

    Link text is escaped and the destination angle-bracketed
    so an untrusted path cannot break out of
    either. `base` defaults to `RLT_BLOB_BASE` from the environment;
    an empty base renders the location as plain code text.
    """
    text = f"{path}:{line}" if line else path
    if base is None:
        base = os.environ.get("RLT_BLOB_BASE", "")
    base = base.rstrip("/")
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


# A finding message carries untrusted repository text. Raw `details` markup in
# it would open a nested disclosure or consume the generated closing tag.
#
# Escaping only the `<` delimiter keeps the literal tag text, including any
# attributes, while leaving the generated wrapper untouched.
_DETAILS_DELIMITER = re.compile(r"<(?=/?details(?=[\s/>]))", re.IGNORECASE)


def escape_details_markup(text):
    """Render a `details` tag delimiter as text so content cannot restructure markup."""
    return _DETAILS_DELIMITER.sub("&lt;", text)


def finding_lines(record, base=None):
    """Markdown lines for one lint finding: bullet, summary, sub-bullets."""
    code = record.get("code", "")
    # Stored snapshots may predate producer-owned titles.
    title = record.get("title") or code
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
    # Codes are emitted by rust-llm-tidy itself, so they need no escaping.
    code_text = f"[`{code}`]({LINT_CODES_URL})" if code else f"`{code}`"
    lines = [f"- **{code_text} {title}** - {location(path, record.get('line'), base)}"]
    lines.append(f"  {summary}")
    lines.extend(f"  - {part}" for part in guidance)
    return [escape_details_markup(line) for line in lines]


def counts_line(errors, warnings, hints, reminders, ai_reminders, changes):
    """Counts of findings and changes, omitting empty groups."""
    parts = []
    for count, noun in ((errors, "error"), (warnings, "warning"),
                        (hints, "hint"), (reminders, "reminder"),
                        (ai_reminders, "AI reminder"), (changes, "change")):
        if count:
            parts.append(f"{count} {noun}" + ("" if count == 1 else "s"))
    return ", ".join(parts) + "."


def render(json_path):
    """The rendered Markdown report; empty when there is nothing to show.

    Missing, unparseable, or non-list documents and empty record lists
    render nothing; the caller falls back to its plain file list.
    """
    try:
        with open(json_path, encoding="utf-8") as fh:
            records = json.load(fh)
    except (OSError, ValueError):
        return ""

    if not isinstance(records, list) or not records:
        return ""

    groups = {severity: [] for severity, _ in FINDING_SECTIONS}
    for record in records:
        severity = record.get("severity")
        if severity in groups:
            groups[severity].append(record)
    changes = [d for d in records if d.get("severity") == "success"]

    out = [counts_line(*(len(groups[severity]) for severity, _ in FINDING_SECTIONS),
                       len(changes))]
    for severity, header in FINDING_SECTIONS:
        group = groups[severity]
        if not group:
            continue
        collapsed = severity == COLLAPSED_SEVERITY
        out.append("")
        if collapsed:
            out.append("<details>")
            out.append(f"<summary>{header}</summary>")
        else:
            out.append(f"### {header}")
        for record in group:
            out.append("")
            out.extend(finding_lines(record))
        if collapsed:
            out.append("")
            out.append("</details>")

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

    return "\n".join(out)


def main(json_path):
    """Print the rendered report for the document at `json_path`."""
    text = render(json_path)
    if text:
        print(text)

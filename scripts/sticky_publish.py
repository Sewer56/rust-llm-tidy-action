#!/usr/bin/env python3
"""Maintain a PR findings report and post summaries of changes.

The "sticky" report is a comment updated in place, not recreated every run.
It groups errors, warnings, and hints separately.
It also stores a hidden snapshot for the next comparison.

Unchanged findings keep links to their original commits.
Added or cleared findings get a separate, compact follow-up comment.
Unchanged results need no comment, unless duplicate reports need consolidation.

# Publication safeguards

- Ownership: updates require the snapshot marker and an accepted bot author.
- Authors: configured logins plus the token's login, if `/user` resolves.
- Revision: check the PR's latest commit matches the scan before writing.
- Encoding: base64 JSON keeps finding text inside the hidden HTML comment.
- Failed scan: skip publication instead of reporting all-clear.
- Size limit: mark oversized snapshots as overflow to avoid false clearances.

# Usage

  sticky_publish.py --findings <run.json> --revision <sha>
      [--repository owner/name] [--pr N] [--server-url URL]
      [--login NAME]...

Environment fallbacks:
- `GITHUB_REPOSITORY`: repository in owner/name form
- `PR_NUMBER`: pull request number
- `GITHUB_SERVER_URL`: server URL, defaulting to https://github.com
- `RLT_STICKY_LOGIN`: accepted bot login, defaulting to `github-actions[bot]`

# Remarks

Delivery uses bounded retries, but does not guarantee exactly-once comments.
The snapshot is saved first, so a failed follow-up does not lose findings.

After snapshot overflow, the next run saves a new baseline.
It does not report changes against the incomplete history.

Exit status:
- `0`: outcome handled, including a publication failure
- `2`: missing required arguments or invalid repository, PR number, or revision
"""

import argparse
import base64
import json
import os
import re
import sys

import finding_compare
import gh_api
import json_table

# Snapshot marker doubles as the sticky-comment identifier. The snapshot
# line is appended last, and decoding uses the last marker occurrence,
# so marker-lookalike text inside finding bullets cannot shadow it.
MARKER = "<!-- rlt-sticky:v1:"
SCHEMA_VERSION = 1

# Conservative payload budget. GitHub documents a 65,536-character issue
# comment limit; staying clearly under it leaves room for transport
# overhead. This is a conservative bound, not a measured repository limit.
#
# Visible bullets yield remaining budget to the snapshot first:
# state integrity outranks display.
MAX_COMMENT_CHARS = 60_000

# Comment scanning is bounded: at most this many 100-item pages. A PR
# with more comments than this is treated as having no sticky report,
# and a fresh one is created.
COMMENT_PAGES = 50
PER_PAGE = 100

DEFAULT_LOGIN = "github-actions[bot]"
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_REVISION = re.compile(r"^[0-9a-fA-F]{7,64}$")

_SECTIONS = (("error", "Errors"), ("warning", "Warnings"),
             ("hint", "Hints - consider looking at these"))
_TRUNCATED_NOTE = "- … and {count} more not shown (report size budget)"
_OVERFLOW_NOTE = (
    "> Report budget exceeded: the list below is truncated, and the next run"
    " re-baselines without a change report."
)


def encode_state(state):
    """The full hidden snapshot comment line for `state`."""
    payload = json.dumps(state, separators=(",", ":"), ensure_ascii=True)
    token = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    return f"{MARKER}{token} -->"


def decode_state(body):
    """The decoded snapshot state in `body`; `None` when absent or invalid.

    Corrupt markup, undecodable base64, wrong schema or malformed
    entries all yield `None` so the caller re-baselines instead of
    trusting invented history.
    """
    start = body.rfind(MARKER)
    if start < 0:
        return None
    rest = body[start + len(MARKER):]
    end = rest.find("-->")
    if end < 0:
        return None
    try:
        state = json.loads(base64.b64decode(rest[:end].strip(), validate=True))
    except ValueError:
        return None
    if not isinstance(state, dict) or state.get("schema") != SCHEMA_VERSION:
        return None
    if not isinstance(state.get("revision"), str):
        return None
    if not isinstance(state.get("overflow", False), bool):
        return None
    entries = state.get("findings")
    if not isinstance(entries, list):
        return None
    for stored in entries:
        if not (
            isinstance(stored, dict)
            and isinstance(stored.get("record"), dict)
            and isinstance(stored.get("revision"), str)
        ):
            return None
    return state


def _join_len(lines):
    """Exact length of the `"\n".join(lines)` the comment body will use."""
    return sum(len(line) for line in lines) + len(lines) - 1


def _append_bounded(lines, sections, visible_budget):
    """Append `(header, bullets)` sections to `lines`, truncated to fit.

    `visible_budget` is the exact join length the appended lines may grow
    `lines` to.

    Every line - headers included - is accounted at its real
    join cost (separator plus text), so the returned hidden count is the
    only thing the caller must still make room for.
    """
    total = sum(len(bullets) for _, bullets in sections)
    used = _join_len(lines)
    shown = 0
    for header, bullets in sections:
        head = f"### {header}"
        if used + 2 + len(head) > visible_budget:
            return total - shown
        lines += ["", head]
        used += 2 + len(head)
        for bullet in bullets:
            if used + 1 + len(bullet) > visible_budget:
                return total - shown
            lines.append(bullet)
            used += 1 + len(bullet)
            shown += 1
    return 0


def _bullet(record, revision, server_url, repository):
    """One compact `code title - location` bullet line for a finding."""
    base = f"{server_url.rstrip('/')}/{repository}/blob/{revision}/"
    return json_table.finding_lines(record, base)[0]


def _sections(entries, server_url, repository):
    """Per-severity bullet lists, severities missing a group skipped."""
    grouped = {severity: [] for severity, _ in _SECTIONS}
    for stored in entries:
        severity = stored["record"].get("severity")
        if severity in grouped:
            grouped[severity].append(
                _bullet(stored["record"], stored["revision"], server_url, repository)
            )
    return [
        (header, grouped[severity])
        for severity, header in _SECTIONS
        if grouped[severity]
    ]


def render_sticky(entries, revision, server_url, repository, budget=MAX_COMMENT_CHARS):
    """Body and state for the sticky comment, bounded to `budget` chars.

    Falls back to an explicit overflow state when the full snapshot
    cannot fit; visible bullets are truncated to whatever budget the
    snapshot leaves, with the counts line always exact.
    """
    state = {
        "schema": SCHEMA_VERSION,
        "revision": revision,
        "overflow": False,
        "findings": entries,
    }
    body = _sticky_body(entries, state, False, server_url, repository, budget)
    if len(body) <= budget:
        return body, state
    state = {
        "schema": SCHEMA_VERSION,
        "revision": revision,
        "overflow": True,
        "findings": [],
    }
    return _sticky_body(entries, state, True, server_url, repository, budget), state


def _sticky_body(entries, state, overflow, server_url, repository, budget):
    lines = ["## rust-llm-tidy: current findings", ""]
    if not entries:
        lines.append("No current findings.")
    else:
        counts = {"error": 0, "warning": 0, "hint": 0}
        for stored in entries:
            severity = stored["record"].get("severity")
            if severity in counts:
                counts[severity] += 1
        lines.append(
            json_table.counts_line(
                counts["error"], counts["warning"], counts["hint"], 0
            )
        )
    if overflow:
        lines += ["", _OVERFLOW_NOTE]

    # The snapshot claims its budget first (the exact join cost of the
    # closing blank line plus snapshot line); only bullets are truncated.
    #
    # Room for the truncation note is reserved at its widest count so
    # appending it can never push the body past `budget`.
    snapshot = encode_state(state)
    sections = _sections(entries, server_url, repository)
    total_bullets = sum(len(bullets) for _, bullets in sections)
    reserve = 1 + len(_TRUNCATED_NOTE.format(count=total_bullets)) \
        if total_bullets else 0
    hidden = _append_bounded(
        lines, sections, budget - len(snapshot) - 2 - reserve
    )
    if hidden:
        lines.append(_TRUNCATED_NOTE.format(count=hidden))

    lines += ["", snapshot]
    return "\n".join(lines)


def render_delta(added, cleared, sticky_url, old_revision, server_url, repository,
                 budget=MAX_COMMENT_CHARS):
    """Follow-up comment body for meaningful changes; `None` when quiet."""
    if not added and not cleared:
        return None
    since = f" since `{old_revision[:7]}`" if old_revision else ""
    lines = [
        "## rust-llm-tidy: findings changed",
        "",
        f"{len(added)} added, {len(cleared)} cleared{since}.",
        "",
        f"Current findings: [full report]({sticky_url}).",
    ]
    groups = (
        ("Added", [(a["record"], a["revision"]) for a in added]),
        ("Cleared", [(c["record"], c["revision"]) for c in cleared]),
    )
    sections = [
        (header, [_bullet(record, revision, server_url, repository)
                  for record, revision in pairs])
        for header, pairs in groups
        if pairs
    ]
    total_bullets = sum(len(bullets) for _, bullets in sections)
    reserve = 1 + len(_TRUNCATED_NOTE.format(count=total_bullets)) \
        if total_bullets else 0
    hidden = _append_bounded(lines, sections, budget - reserve)
    if hidden:
        lines.append(_TRUNCATED_NOTE.format(count=hidden))
    return "\n".join(lines)


def _list_comments(transport, repository, pr, max_pages):
    """All PR conversation comments, scanning at most `max_pages`."""
    comments = []
    for page in range(1, max_pages + 1):
        batch = transport.request(
            "GET",
            f"repos/{repository}/issues/{pr}/comments?per_page={PER_PAGE}&page={page}",
        )
        if not isinstance(batch, list):
            raise gh_api.TransportError(None, "unexpected comment list response")
        comments.extend(batch)
        if len(batch) < PER_PAGE:
            break
    return comments


def _owned_stickies(comments, logins):
    """Marker-carrying comments authored by an expected bot identity."""
    owned = []
    for comment in comments:
        body = comment.get("body")
        user = comment.get("user") or {}
        if isinstance(body, str) and MARKER in body and user.get("login") in logins:
            owned.append(comment)
    return sorted(owned, key=lambda comment: comment.get("id", 0))


def _pr_head(transport, repository, pr):
    """The PR's current head SHA; raises TransportError when unreadable."""
    pull = transport.request("GET", f"repos/{repository}/pulls/{pr}")
    return (pull or {}).get("head", {}).get("sha")


def publish(transport, repository, pr, revision, records, server_url, logins,
            budget=MAX_COMMENT_CHARS, max_pages=COMMENT_PAGES):
    """Publish current findings; returns a summary dict for logs/tests.

    Never raises for delivery problems: those surface as `status`
    `"failed"` (or `delta_failed`) so callers can warn without changing
    the job outcome. `records` must already be a validated record list;
    `None` (failed collection) skips publication entirely.
    """
    summary = {
        "status": "noop",
        "sticky_url": None,
        "delta_posted": False,
        "delta_failed": False,
        "deleted_extra": 0,
        "reason": None,
    }
    if records is None:
        summary.update(status="failed", reason="findings document missing or invalid")
        return summary

    def stale():
        # The stale-run gate: the PR head must still be the revision this
        # run describes, or a slower run would overwrite newer state.
        # Checked at entry and again right before the write, because the
        # paginated comment scan in between can take seconds.
        try:
            head = _pr_head(transport, repository, pr)
        except gh_api.TransportError as exc:
            summary.update(status="failed", reason=str(exc))
            return summary
        if head != revision:
            summary.update(status="stale", reason=f"PR head moved to {head}")
            return summary
        return None

    problem = stale()
    if problem:
        return problem

    logins = set(logins)
    try:
        user = transport.request("GET", "user")
        if isinstance(user, dict) and user.get("login"):
            logins.add(user["login"])
    except gh_api.TransportError:
        pass  # identity resolution is advisory; the configured set stands
    try:
        owned = _owned_stickies(
            _list_comments(transport, repository, pr, max_pages), logins
        )
    except gh_api.TransportError as exc:
        summary.update(status="failed", reason=str(exc))
        return summary

    current = finding_compare.findings(records)
    baseline = decode_state(owned[-1]["body"]) if owned else None
    if baseline and baseline.get("overflow"):
        # Never diff against a truncated list; re-baseline instead.
        baseline = None
    if baseline is not None:
        retained, added, cleared = finding_compare.merge_state(
            baseline["findings"], current, revision
        )
    else:
        retained, added, cleared = [], [
            finding_compare.entry(record, revision) for record in current
        ], []

    if not owned and not current:
        summary["reason"] = "no findings and no existing report"
        return summary

    # Unchanged findings write nothing - unless a concurrent run left
    # duplicate stickies; consolidating those still counts as an update.
    consolidate = len(owned) > 1
    if baseline is not None and not added and not cleared and not consolidate:
        summary["status"] = "unchanged"
        return summary

    entries = retained + added
    body, _ = render_sticky(entries, revision, server_url, repository, budget)
    sticky_id = owned[-1].get("id") if owned else None

    # Re-check right before writing: this run must not overwrite state a
    # newer revision published while the comment scan was running.
    problem = stale()
    if problem:
        return problem

    try:
        if sticky_id is not None:
            comment = transport.request(
                "PATCH",
                f"repos/{repository}/issues/comments/{int(sticky_id)}",
                {"body": body},
            )
        else:
            comment = transport.request(
                "POST", f"repos/{repository}/issues/{pr}/comments", {"body": body}
            )
    except gh_api.TransportError as exc:
        summary.update(status="failed", reason=str(exc))
        return summary
    summary["status"] = "updated" if sticky_id is not None else "created"
    summary["sticky_url"] = (comment or {}).get("html_url")

    # Follow-up report only for real changes against a real baseline:
    # first publication (and re-baselining after deletion, corruption or
    # overflow) stays quiet so there is no added-report flood.
    if baseline is not None and (added or cleared):
        delta = render_delta(
            added, cleared, summary["sticky_url"], baseline["revision"],
            server_url, repository, budget,
        )
        try:
            transport.request(
                "POST", f"repos/{repository}/issues/{pr}/comments", {"body": delta}
            )
            summary["delta_posted"] = True
        except gh_api.TransportError:
            summary["delta_failed"] = True

    # Concurrent runs can both create a report; consolidate to the newest.
    for extra in owned[:-1]:
        try:
            transport.request(
                "DELETE", f"repos/{repository}/issues/comments/{int(extra['id'])}"
            )
            summary["deleted_extra"] += 1
        except gh_api.TransportError:
            pass
    return summary


def _env_fallbacks(args):
    """Resolve CLI args with the action's env fallbacks."""
    repository = args.repository or os.environ.get("GITHUB_REPOSITORY", "")
    pr = args.pr or os.environ.get("PR_NUMBER", "")
    server_url = (
        args.server_url or os.environ.get("GITHUB_SERVER_URL") or "https://github.com"
    )
    logins = set(args.login)
    logins.add(os.environ.get("RLT_STICKY_LOGIN") or DEFAULT_LOGIN)
    return repository, pr, server_url, logins


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--findings", required=True, help="run JSON document path")
    parser.add_argument("--revision", required=True, help="revision the findings describe")
    parser.add_argument("--repository", help="owner/name (env GITHUB_REPOSITORY)")
    parser.add_argument("--pr", help="pull request number (env PR_NUMBER)")
    parser.add_argument("--server-url", help="git server base URL")
    parser.add_argument(
        "--login", action="append", default=[],
        help="accepted bot identity (repeatable)",
    )
    args = parser.parse_args(argv)
    repository, pr, server_url, logins = _env_fallbacks(args)
    if not _REPOSITORY.match(repository) or not pr.isdigit() \
            or not _REVISION.match(args.revision):
        print("::error::rust-llm-tidy: invalid repository, PR number or revision",
              file=sys.stderr)
        return 2

    records = finding_compare.load_records(args.findings)
    if records is None:
        print("::warning::rust-llm-tidy: findings document missing or invalid;"
              " skipping report publication")
        return 0

    summary = publish(
        gh_api.GhApi(), repository, int(pr), args.revision, records,
        server_url, logins,
    )
    if summary["status"] in ("created", "updated"):
        note = "published findings report"
        if summary["delta_posted"]:
            note += " with a change report"
        print(f"rust-llm-tidy: {note} {summary['sticky_url']}")
    elif summary["status"] == "stale":
        print(f"::notice::rust-llm-tidy: {summary['reason']}; skipping stale report")
    elif summary["status"] == "failed":
        print(f"::warning::rust-llm-tidy: could not publish findings report"
              f" ({summary['reason']})")
    if summary["delta_failed"]:
        print("::warning::rust-llm-tidy: could not post the change report comment")
    return 0


if __name__ == "__main__":
    sys.exit(main())

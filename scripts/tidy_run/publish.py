"""Publication of run outcomes: sticky findings, head rescans, PR comments.

Everything here is best-effort: a reporting problem warns and never
changes the run's exit status. Validate runs collect no findings, so
they skip publication entirely.
"""

import subprocess
import sys

import head_view
import sticky_publish
from reporting import json_table


def publish_findings(findings_path, revision, validate):
    """Publish the sticky findings report for `revision`.

    `sticky_publish.py` resolves the repository and PR from the
    environment; its own diagnostics explain a failed publication.
    """
    if validate:
        return
    status = sticky_publish.main(
        ["--findings", str(findings_path), "--revision", revision]
    )
    if status != 0:
        print("::warning::rust-llm-tidy: could not publish the findings"
              " report", file=sys.stderr)


def collect_head_findings(binary, head_json, scan_args, revision):
    """Collect findings at a committed revision; `False` when it failed.

    The head view scans a temporary checkout without editing it; a
    failed collection must skip publication, not report an all-clear.
    """
    return head_view.scan_revision(
        ".", revision, binary, head_json, scan_args
    ) == 0


def post_findings_comment(comment_path, run_json, pr_number):
    """Post the 'not tidy' comment rendered from the run's JSON document.

    Without a renderable document nothing is posted; the run still fails.
    """
    table = json_table.render(run_json) if run_json.exists() else ""
    if table:
        body = f"## rust-llm-tidy: not tidy\n\n{table}\n"
        _post_comment(comment_path, body, pr_number, "could not post report"
                      " comment")


def post_push_failure_comment(comment_path, files, pr_number):
    """Post the fixed-file list for a fix commit that could not be pushed."""
    bullets = "\n".join(f"  - `{name}`" for name in files)
    body = (
        "## rust-llm-tidy: could not push fixes\n"
        "\n"
        "I tidied the files below but could not push the fix commit"
        " (fork PR or the\ntoken lacks write access to the PR branch).\n"
        "\n"
        "Fixed files:\n"
        f"{bullets}\n"
        "\n"
        "To get the fixes applied, re-run with a token that can write to"
        " the PR branch\n(e.g. `permissions: contents: write`, or a PAT on"
        " fork PRs via\n`pull_request_target`).\n"
    )
    _post_comment(comment_path, body, pr_number, "could not post failure"
                  " comment")


def _post_comment(comment_path, body, pr_number, warning):
    """Write the comment file and post it; a posting failure only warns."""
    comment_path.write_text(body)
    posted = subprocess.run(
        ["gh", "pr", "comment", pr_number, "--body-file", str(comment_path)]
    )
    if posted.returncode != 0:
        print(f"::warning::rust-llm-tidy: {warning}", file=sys.stderr)

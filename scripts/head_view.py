#!/usr/bin/env python3
"""Scan the PR's committed code without changing the runner's checkout.

The runner's checkout does not always match the PR head:

- Default PR checkouts scan a merge commit whose bytes can differ
  from the head's.
- A failed apply push leaves a local fix commit that never reached the PR.

The report must describe the PR branch's latest commit, called its head,
not those scans.

This script creates a temporary checkout using `git worktree`.
It scans the requested commit with `--checks-only`.
The runner's existing checkout stays untouched.

# Usage

  head_view.py --repository-dir DIR --revision SHA --args-file PATH
      --binary PATH --output PATH

- `--repository-dir`: the main run's project directory, possibly below repo root
- `--revision`: a hexadecimal commit ID, never a branch name or Git option
- `--args-file`: the main run's arguments, separated and terminated by NUL bytes
- `--binary`: the rust-llm-tidy executable
- `--output`: destination for the collected JSON record list

The scan reuses the saved arguments and matching project subdirectory,
with any saved `--dry-run` replaced by `--checks-only`.

# Remarks

A revision containing `.gitmodules` is rejected: fetching PR-controlled
submodule URLs is unsafe, and skipping submodules could falsely clear findings.

Collection failures exit nonzero so the caller skips publication, rather
than treating missing results as an all-clear report.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

_REVISION = re.compile(r"^[0-9a-fA-F]{4,64}$")


def _git(repository_dir, *argv):
    """Run Git and capture its output without raising for a nonzero exit."""
    return subprocess.run(
        ["git", "-C", repository_dir, *argv], capture_output=True, text=True
    )


def _scan_worktree(repository_dir, scan_args, binary, output, worktree):
    """Write findings from the temporary checkout; return 1 when collection fails.

    Run from the matching project subdirectory so relative paths keep their meaning.
    A nonzero tidy exit is acceptable if stdout contains a JSON record list.
    """
    # Never fetch PR-controlled submodule URLs. Missing submodule files could
    # make existing findings look fixed, so reject an incomplete checkout.
    if os.path.exists(os.path.join(worktree, ".gitmodules")):
        print(
            "::warning::rust-llm-tidy: head view cannot cover"
            " submodules; skipping report publication",
            file=sys.stderr,
        )
        return 1

    prefix = _git(repository_dir, "rev-parse", "--show-prefix")
    if prefix.returncode != 0:
        print(
            "::warning::rust-llm-tidy: could not resolve the project"
            " directory for the head view",
            file=sys.stderr,
        )
        return 1

    # Findings-only rescan: --checks-only runs the lints without editing
    # or post-processing, and its exit status stays about findings, not
    # proposed edits.
    #
    # A saved --dry-run (check mode) would change that, so the rescan
    # strips it. The flag goes before the `--` target separator so it
    # stays a flag even when targets contain flag-shaped names.
    split = scan_args.index("--") if "--" in scan_args else len(scan_args)
    head = [a for a in scan_args[:split] if a != "--dry-run"]
    scan_argv = head + ["--checks-only"] + scan_args[split:]
    try:
        run = subprocess.run(
            [binary, *scan_argv],
            cwd=os.path.join(worktree, prefix.stdout.strip()),
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        print(
            f"::warning::rust-llm-tidy: could not run the tidy binary: {exc}",
            file=sys.stderr,
        )
        return 1

    # Remaining findings can cause a nonzero exit. Validate the output instead.
    try:
        records = json.loads(run.stdout)
    except ValueError:
        print(
            "::warning::rust-llm-tidy: head view produced no JSON document;"
            " skipping report publication",
            file=sys.stderr,
        )
        return 1
    if not isinstance(records, list):
        print(
            "::warning::rust-llm-tidy: head view document is not a record"
            " list; skipping report publication",
            file=sys.stderr,
        )
        return 1

    with open(output, "w", encoding="utf-8") as output_file:
        json.dump(records, output_file)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repository-dir", required=True, help="checkout to scan")
    parser.add_argument("--revision", required=True, help="commit id to scan")
    parser.add_argument("--args-file", required=True, help="NUL-terminated main argv")
    parser.add_argument("--binary", required=True, help="rust-llm-tidy binary")
    parser.add_argument("--output", required=True, help="findings JSON to write")
    args = parser.parse_args(argv)

    try:
        with open(args.args_file, "rb") as argument_file:
            saved_args = [
                chunk.decode("utf-8")
                for chunk in argument_file.read().split(b"\0")
                if chunk
            ]
    except OSError as exc:
        print(
            f"::warning::rust-llm-tidy: unreadable arguments file: {exc}",
            file=sys.stderr,
        )
        return 1

    return scan_revision(
        args.repository_dir, args.revision, args.binary, args.output,
        saved_args,
    )


def scan_revision(repository_dir, revision, binary, output, scan_args):
    """Scan one committed revision in a throwaway worktree.

    The internal entry point for in-process callers; `main` wraps it
    with the NUL-separated-arguments CLI. Returns 0 only after writing
    a record list to `output`.
    """
    if not _REVISION.match(revision):
        print(
            f"::warning::rust-llm-tidy: refusing non-commit revision {revision!r}",
            file=sys.stderr,
        )
        return 1

    scratch = tempfile.mkdtemp(prefix="rlt-head-view-")
    worktree = f"{scratch}/w"
    try:
        added = _git(
            repository_dir, "worktree", "add", "--detach", "--quiet",
            worktree, revision,
        )
        if added.returncode != 0:
            print(
                f"::warning::rust-llm-tidy: could not create the head view:"
                f" {added.stderr.strip()}",
                file=sys.stderr,
            )
            return 1
        try:
            return _scan_worktree(repository_dir, scan_args, binary,
                                  output, worktree)
        finally:
            _git(repository_dir, "worktree", "remove", "--force", worktree)
            _git(repository_dir, "worktree", "prune")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

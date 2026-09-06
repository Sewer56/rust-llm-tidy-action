#!/usr/bin/env python3
"""Collect the committed PR-head findings without touching the checkout.

After a check-mode failure or a failed apply push, the working tree holds
uncommitted tidy fixes, so the main run's findings describe content the PR
does not have. This helper re-collects findings for the revision the PR
actually points at:

- a temporary detached `git worktree` is checked out at that revision
  (the main checkout and its dirty files are never touched);
- the tidy binary runs there in read-only `--dry-run` mode from the same
  project subdirectory the main run used (`--repository-dir` may sit
  below the repository root), reusing the exact argument list (paths,
  config, filters), so the comparison sees the same files, rules and
  record paths;
- stdout must parse as the tool's JSON document; anything else (worktree
  failure, project-directory resolution failure, spawn failure, missing or
  unparseable output, or a `.gitmodules` at the collected revision, whose
  submodule content the worktree cannot cover without fetching
  PR-controlled URLs) exits non-zero and the caller skips publication
  rather than reporting findings that were never collected.

CLI:
  head_view.py --repository-dir DIR --revision SHA --args-file PATH
      --binary PATH --output PATH

The arguments file holds the main run's NUL-terminated argv (the action's
arguments step writes it); `--dry-run` is appended here. `--revision`
must be a plain hex commit id so it can never be mistaken for a git
option.
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
    """One git call; the completed process (never raises)."""
    return subprocess.run(
        ["git", "-C", repository_dir, *argv], capture_output=True, text=True
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repository-dir", required=True, help="checkout to scan")
    parser.add_argument("--revision", required=True, help="commit id to scan")
    parser.add_argument("--args-file", required=True, help="NUL-terminated main argv")
    parser.add_argument("--binary", required=True, help="rust-llm-tidy binary")
    parser.add_argument("--output", required=True, help="findings JSON to write")
    args = parser.parse_args(argv)

    if not _REVISION.match(args.revision):
        print(
            f"::warning::rust-llm-tidy: refusing non-commit revision {args.revision!r}",
            file=sys.stderr,
        )
        return 1

    scratch = tempfile.mkdtemp(prefix="rlt-head-view-")
    worktree = f"{scratch}/w"
    try:
        added = _git(args.repository_dir, "worktree", "add", "--detach", "--quiet",
                     worktree, args.revision)
        if added.returncode != 0:
            print(
                f"::warning::rust-llm-tidy: could not create the head view:"
                f" {added.stderr.strip()}",
                file=sys.stderr,
            )
            return 1
        try:
            # Deliberately no `git submodule update --init` here: the
            # `.gitmodules` at the PR head is PR-controlled and alone
            # selects which URL the runner would fetch. Without the init
            # the worktree holds no submodule content, so a repository
            # that lints inside submodules would yield a partial document
            # (its findings would later read as cleared); fail closed
            # instead of collecting an incomplete view.
            if os.path.exists(os.path.join(worktree, ".gitmodules")):
                print(
                    "::warning::rust-llm-tidy: head view cannot cover"
                    " submodules; skipping report publication",
                    file=sys.stderr,
                )
                return 1

            try:
                with open(args.args_file, "rb") as fh:
                    saved = [chunk.decode("utf-8") for chunk in fh.read().split(b"\0")
                             if chunk]
            except OSError as exc:
                print(f"::warning::rust-llm-tidy: unreadable arguments file: {exc}",
                      file=sys.stderr)
                return 1

            # Saved paths are relative to the main run's project
            # directory, so the worktree run must start from the matching
            # subdirectory or the collected document is rooted at the
            # wrong directory.
            prefix = _git(args.repository_dir, "rev-parse", "--show-prefix")
            if prefix.returncode != 0:
                print(
                    "::warning::rust-llm-tidy: could not resolve the project"
                    " directory for the head view",
                    file=sys.stderr,
                )
                return 1

            try:
                run = subprocess.run(
                    [args.binary, *saved, "--dry-run"],
                    cwd=os.path.join(worktree, prefix.stdout.strip()),
                    capture_output=True, text=True,
                )
            except OSError as exc:
                print(f"::warning::rust-llm-tidy: could not run the tidy binary: {exc}",
                      file=sys.stderr)
                return 1

            # A non-zero exit means findings remain; that is a valid
            # collected document. Only an unusable document is a failure.
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
            with open(args.output, "w", encoding="utf-8") as fh:
                json.dump(records, fh)
        finally:
            _git(args.repository_dir, "worktree", "remove", "--force", worktree)
            _git(args.repository_dir, "worktree", "prune")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Git operations for the tidy run, each an explicit argv."""

import subprocess
import sys
from pathlib import Path

from . import StepError

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
BASELINE_HELPER = SCRIPTS_DIR / "ensure_diff_base.sh"


def _decode(raw):
    return raw.decode("utf-8", "surrogateescape")


def _git(*argv, check=True):
    """Run git in the current directory, capturing its output.

    A checked failure raises `StepError` carrying git's diagnostics and
    exit status; `check=False` leaves the status to the caller.
    """
    proc = subprocess.run(["git", *argv], capture_output=True)
    if check and proc.returncode != 0:
        stderr = _decode(proc.stderr).strip()
        raise StepError(
            f"git {' '.join(argv)} failed:\n{stderr}", exit_code=proc.returncode
        )
    return proc


def tree_is_clean():
    """Whether the checkout has no staged, unstaged, or untracked edits."""
    status = _git("status", "--porcelain", "--untracked-files=all")
    return not status.stdout.strip()


def changed_paths():
    """Sorted unique paths the run changed: tracked diffs plus untracked files.

    NUL-separated output keeps paths with newlines or spaces intact.
    A failing diff is tolerated like the shell's `|| true`; untracked
    listing failures fail the step.
    """
    tracked = _git("diff", "--name-only", "-z", "HEAD", check=False)
    untracked = _git("ls-files", "--others", "--exclude-standard", "-z")
    names = set()
    for proc in (tracked, untracked):
        names.update(_decode(name) for name in proc.stdout.split(b"\0") if name)
    return sorted(names)


def head_commit():
    """The current commit id."""
    return _decode(_git("rev-parse", "HEAD").stdout).strip()


def ensure_baseline(base):
    """Make the diff baseline resolvable without changing the checkout.

    The helper reports its own errors; its exit status fails the step.
    """
    proc = subprocess.run(["bash", str(BASELINE_HELPER), base])
    if proc.returncode != 0:
        raise StepError(exit_code=proc.returncode)


def commit_paths(paths, name, email):
    """Commit the fixed paths on behalf of the configured bot identity."""
    if not name or not email:
        raise StepError(
            "COMMIT_NAME and COMMIT_EMAIL must name the fix commit's author"
        )
    _git("config", "user.name", name)
    _git("config", "user.email", email)
    _git("add", "--", *paths)
    _git("commit", "-m", "Apply rust-llm-tidy fixes",
         "-m", "Automated by the rust-llm-tidy GitHub Action.")


def push_head(head_ref):
    """Whether the fix commit pushed to the PR branch.

    A failed push prints git's diagnostics; the caller comments and fails.
    """
    proc = _git("push", "origin", f"HEAD:refs/heads/{head_ref}", check=False)
    if proc.returncode != 0:
        sys.stderr.write(_decode(proc.stderr))
    return proc.returncode == 0

"""Processing-target selection: explicit paths, PR changed files, or '.'."""

import subprocess


class TargetError(Exception):
    """Target resolution failed.

    `exit_code` is the step's exit status.
    """

    def __init__(self, message="", exit_code=1):
        super().__init__(message)
        self.exit_code = exit_code


def target_paths(parsed, project_dir):
    """Path arguments: explicit paths win, then the PR's changed files,
    then the whole project directory."""
    if parsed.path_list:
        return _path_list(parsed.path_list)
    if parsed.changed_files:
        if parsed.default_files:
            return _pr_changed_files(parsed.default_files, project_dir)
        # Non-PR runs: the CLI falls back to the working-tree git diff.
        return []
    return ["."]


def _path_list(raw):
    return [line.strip() for line in raw.split("\n") if line.strip()]


def _pr_changed_files(base, project_dir):
    """The files this PR changed against `base`.

    The orchestrator has already made `base` resolvable. NUL-separated
    names keep paths with newlines or spaces intact.
    """
    diff = subprocess.run(
        ["git", "diff", "--name-only", "-z", "--diff-filter=ACMR",
         f"{base}...HEAD"],
        cwd=project_dir, capture_output=True,
    )
    if diff.returncode != 0:
        raise TargetError(
            f"changed-files: could not compare PR base {base} with HEAD"
        )
    return [
        name for name in
        diff.stdout.decode("utf-8", "surrogateescape").split("\0")
        if name
    ]

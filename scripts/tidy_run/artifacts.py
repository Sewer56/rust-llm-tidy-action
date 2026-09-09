"""Run-step artifact paths under `$RUNNER_TEMP`."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Artifacts:
    """The files the run step exchanges with its helpers."""

    run_json: Path
    run_err: Path
    head_json: Path
    comment: Path
    fixed_files: Path

    @classmethod
    def under(cls, runner_temp):
        """The artifact paths inside `runner_temp`."""
        return cls(
            run_json=runner_temp / "rlt-run.json",
            run_err=runner_temp / "rlt-run.err",
            head_json=runner_temp / "rlt-head-run.json",
            comment=runner_temp / "rlt-comment.md",
            fixed_files=runner_temp / "rlt-fixed-files",
        )

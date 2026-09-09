"""Validated environment settings for the tidy run step."""

from dataclasses import dataclass
from pathlib import Path

from step_inputs import boolean, run_mode, text

from . import StepError


@dataclass(frozen=True)
class RunSettings:
    """The run step's inputs, read from the action's environment."""

    binary: str
    mode: str
    is_pr: bool
    validate: bool
    pr_base: str
    pr_number: str
    head_ref: str
    commit_name: str
    commit_email: str
    head_sha: str
    runner_temp: Path

    @classmethod
    def from_env(cls, environ):
        """Validated settings from `environ`; raises `StepError`."""
        runner_temp = text(environ, "RUNNER_TEMP")
        if not runner_temp:
            raise StepError("RUNNER_TEMP must name the runner's temp dir")
        binary = text(environ, "RLT_BIN")
        if not binary:
            raise StepError("RLT_BIN must name the tidy binary")

        config = cls(
            binary=binary,
            mode=run_mode(environ),
            is_pr=boolean(environ, "IS_PR"),
            validate=boolean(environ, "RLT_VALIDATE"),
            pr_base=text(environ, "RLT_PR_BASE"),
            pr_number=text(environ, "PR_NUMBER"),
            head_ref=text(environ, "HEAD_REF"),
            commit_name=text(environ, "COMMIT_NAME"),
            commit_email=text(environ, "COMMIT_EMAIL"),
            head_sha=text(environ, "RLT_HEAD_SHA"),
            runner_temp=Path(runner_temp),
        )
        if config.is_pr:
            if not config.pr_number.isdigit():
                raise StepError(
                    "PR_NUMBER must be the pull request number on PR runs,"
                    f" got {config.pr_number!r}"
                )
            if not config.head_ref:
                raise StepError("HEAD_REF must name the PR branch on PR runs")
        return config

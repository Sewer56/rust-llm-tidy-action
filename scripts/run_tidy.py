#!/usr/bin/env python3
"""Run the tidy binary and drive the action's PR workflow from its outcome.

Builds the tidy command line from the action's inputs (flags, op
inclusions/exclusions, processing targets), then runs it.

Check-style runs stay read-only and pass the tool's exit status through.
Apply runs commit and push fixes on PRs, or leave them in the working tree
elsewhere.

PR failures post a comment and publish the sticky findings report,
collected from the PR head whenever the checkout is not the head
itself.

# Errors

- `1`: invalid environment input, the binary lacks --checks-only, a PR
  checkout is dirty, or a push failed after fixes.
- Git's own exit status when a run-step git operation fails; `1` when
  the fix-commit identity is missing.
- The baseline helper's own exit status when it cannot make the PR base
  resolvable.
- The tool's own exit status otherwise, including validate runs.
"""

import os
import sys

from step_inputs import EnvInputError
from tidy_args import build_argv, inputs, targets
from tidy_run import StepError, artifacts, capability, gitops, publish
from tidy_run import runner, settings


def main() -> int:
    try:
        config = settings.RunSettings.from_env(os.environ)
        return _run(config)
    except (StepError, EnvInputError, targets.TargetError) as exc:
        if str(exc):
            print(f"::error::rust-llm-tidy: {exc}", file=sys.stderr)
        return exc.exit_code


def _run(config):
    files = artifacts.Artifacts.under(config.runner_temp)

    parsed = inputs.BuilderInputs.from_env(os.environ)

    capability.require_checks_only(config.binary)

    # Never commit or publish pre-existing edits as committed findings.
    if config.is_pr and not config.validate and not gitops.tree_is_clean():
        raise StepError("PR runs require a clean checkout; commit or stash"
                        " existing edits")

    _share_baseline(config, parsed)

    argv = build_argv(parsed, os.getcwd())

    tidy_rc = runner.capture(
        config.binary, argv, files.run_json, files.run_err
    )

    if config.validate:
        return tidy_rc

    # Files this run changed (the fixes), if any.
    changed = gitops.changed_paths()

    if parsed.read_only or not changed:
        return _report_without_commit(config, files, argv, changed, tidy_rc)

    files.fixed_files.write_text("\n".join(changed) + "\n")

    if config.mode == "apply" and config.is_pr:
        return _push_fixes(config, files, argv, changed, tidy_rc)

    # apply, non-PR: leave the fixes in the working tree.
    return tidy_rc


def _share_baseline(config, parsed):
    """Export the PR baseline and make each diff base resolvable.

    Older binaries ignore the variable; explicit overrides win, and an
    empty override is replaced like an unset one. The baseline helper
    makes the merge-base resolvable without changing the checkout.

    The helper runs here once, before argv building and the head-view
    rescan reuse the base.
    """
    if config.is_pr and config.pr_base \
            and not os.environ.get("RUST_LLM_TIDY_DIFF_BASE"):
        os.environ["RUST_LLM_TIDY_DIFF_BASE"] = config.pr_base
    if config.validate:
        return
    bases = [os.environ.get("RUST_LLM_TIDY_DIFF_BASE", "")]
    if parsed.changed_files:
        bases.append(parsed.default_files)
    for base in dict.fromkeys(b for b in bases if b):
        gitops.ensure_baseline(base)


def _report_without_commit(config, files, argv, changed, tidy_rc):
    """Report a read-only or clean-tree run; read-only runs never commit."""
    if config.is_pr and tidy_rc != 0:
        publish.post_findings_comment(
            files.comment, files.run_json, config.pr_number
        )

    # Clean tree: publish findings whatever the exit status (hints and
    # warnings do not fail the run but still belong in the report).
    #
    # When HEAD is not the PR head (the default PR checkout scans a merge
    # commit), collect findings at the head and publish those. A failed
    # collection skips publication rather than misreporting merge bytes
    # as the head's.
    if config.is_pr and not changed:
        head = gitops.head_commit()
        if config.head_sha and head != config.head_sha:
            collected = publish.collect_head_findings(
                config.binary, files.head_json, argv, config.head_sha
            )
            if collected:
                publish.publish_findings(
                    files.head_json, config.head_sha, config.validate
                )
        else:
            publish.publish_findings(
                files.run_json, head, config.validate
            )
    return tidy_rc


def _push_fixes(config, files, argv, changed, tidy_rc):
    """apply + PR: commit and push the fixes; comment and fail otherwise."""
    gitops.commit_paths(changed, config.commit_name, config.commit_email)

    # Plain GITHUB_TOKEN can't write a fork's PR branch, so push fails.
    if gitops.push_head(config.head_ref):
        # The findings describe the pushed fix commit's content exactly.
        publish.publish_findings(
            files.run_json, gitops.head_commit(), config.validate
        )
        return tidy_rc

    publish.post_push_failure_comment(
        files.comment, changed, config.pr_number
    )
    # The fix commit never reached the PR branch: the report describes
    # the PR head, not the local fixes.
    revision = config.head_sha or gitops.head_commit()
    if publish.collect_head_findings(
            config.binary, files.head_json, argv, revision):
        publish.publish_findings(files.head_json, revision, config.validate)
    print("::error::rust-llm-tidy: apply tidied files but could not push",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())

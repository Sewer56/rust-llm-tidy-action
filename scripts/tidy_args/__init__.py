"""Argument-builder helpers: inputs, flags, target selection."""

from pathlib import Path

from . import flags, targets


def build_argv(parsed, project_dir):
    """The full tidy argv; validate runs stop at `--validate`.

    Flag order matches the action's approved contract: config flags,
    JSON output and read-only selection, then include/exclude entries,
    then `--` and the processing targets.

    The separator keeps flag-shaped target names positional instead of
    CLI flags.
    """
    if parsed.validate:
        return ["--validate"]
    args = (
        flags.flag_args(parsed)
        + flags.ops_args("--include", parsed.include)
        + flags.ops_args("--exclude", parsed.exclude)
    )
    paths = targets.target_paths(parsed, Path(project_dir))
    if not paths:
        return args
    return args + ["--", *paths]

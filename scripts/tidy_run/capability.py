"""The --checks-only capability gate for the tidy binary."""

import subprocess

from . import StepError

_NOT_SUPPORTED = (
    "the tidy binary does not support --checks-only; this action requires"
    " it. Build one with binary-source: git (current source), or use a"
    " release that ships --checks-only"
)


def require_checks_only(binary):
    """Fail the step unless `binary --help` runs and lists --checks-only.

    The gate runs before anything mutates: the action rescans committed
    revisions with --checks-only, and binaries without it also exit zero
    for proposed edits.

    --version cannot tell the builds apart, so the read-only --help output
    is probed instead.

    Raises `StepError` with the probe's first diagnostic line when the
    probe fails.
    """
    try:
        probe = subprocess.run(
            [binary, "--help"], capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
    except FileNotFoundError as exc:
        raise StepError(f"--help failed (exit 127): {exc}") from exc
    except PermissionError as exc:
        raise StepError(f"--help failed (exit 126): {exc}") from exc
    except OSError as exc:
        raise StepError(f"--help failed: {exc}") from exc

    combined = probe.stdout + probe.stderr
    if probe.returncode != 0:
        first_line = combined.strip().splitlines()[:1]
        detail = first_line[0] if first_line else ""
        raise StepError(f"--help failed (exit {probe.returncode}): {detail}")
    if "--checks-only" not in combined:
        raise StepError(_NOT_SUPPORTED)

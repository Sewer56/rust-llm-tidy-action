"""Validated environment inputs for the action's run step.

Unset or empty booleans read as false, matching the standalone script
harness; the action itself always exports them. Any other value must be
`true` or `false`.
"""


class EnvInputError(RuntimeError):
    """One environment variable is missing or invalid.

    `exit_code` is the step's exit status.
    """

    exit_code = 1


def boolean(environ, name):
    """A `true`/`false` variable; unset or empty reads as false."""
    raw = environ.get(name, "")
    if not raw:
        return False
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise EnvInputError(f"{name} must be 'true' or 'false', got {raw!r}")


def text(environ, name):
    """A free-form variable; unset reads as empty."""
    return environ.get(name, "")


def run_mode(environ):
    """The validated `RLT_MODE`; unset reads as `apply`."""
    value = text(environ, "RLT_MODE") or "apply"
    if value not in ("apply", "check"):
        raise EnvInputError(
            f"RLT_MODE must be 'apply' or 'check', got {value!r}"
        )
    return value

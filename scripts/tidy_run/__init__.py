"""Run-step helpers: settings, capability gate, execution, git, publication."""


class StepError(RuntimeError):
    """The run step must fail.

    `message` is empty when the failing helper already reported its own
    error; `exit_code` is the step's exit status.
    """

    def __init__(self, message="", exit_code=1):
        super().__init__(message)
        self.exit_code = exit_code

"""Validated environment inputs for the tidy argument builder."""

from dataclasses import dataclass

from step_inputs import boolean, run_mode, text


@dataclass(frozen=True)
class BuilderInputs:
    """The argument-builder's inputs, read from the action step env."""

    validate: bool
    no_config: bool
    dry_run: bool
    mode: str
    config_path: str
    include: str
    exclude: str
    path_list: str
    default_files: str
    changed_files: bool

    @property
    def read_only(self):
        """Whether this run must not modify files (check or dry-run)."""
        return self.dry_run or self.mode == "check"

    @classmethod
    def from_env(cls, environ):
        """Validated inputs from `environ`; raises `EnvInputError`."""
        return cls(
            validate=boolean(environ, "RLT_VALIDATE"),
            no_config=boolean(environ, "RLT_NO_CONFIG"),
            dry_run=boolean(environ, "RLT_DRY_RUN"),
            mode=run_mode(environ),
            config_path=text(environ, "RLT_CONFIG_PATH"),
            include=text(environ, "RLT_INCLUDE"),
            exclude=text(environ, "RLT_EXCLUDE"),
            path_list=text(environ, "RLT_PATH_LIST"),
            default_files=text(environ, "RLT_DEFAULT_FILES"),
            changed_files=boolean(environ, "RLT_CHANGED_FILES"),
        )

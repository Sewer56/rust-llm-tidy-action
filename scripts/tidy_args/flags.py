"""Flag and op-selection arguments for the tidy command line."""


def flag_args(parsed):
    """Config, output, and read-only flags.

    The action consumes lint findings and recorded changes from JSON only:
    both real and dry-run runs emit one JSON document, so JSON output is
    requested unconditionally.
    """
    args = []
    if parsed.no_config:
        args.append("--no-config")
    elif parsed.config_path:
        args += ["--config", parsed.config_path]
    args += ["--output-mode", "json"]
    if parsed.read_only:
        args.append("--dry-run")
    return args


def ops_args(flag, raw):
    """Repeated `flag` arguments from a comma-, semicolon-, or line-separated list.

    Entries are trimmed and empty ones skipped; spaces inside an entry are
    part of its value.
    """
    args = []
    for line in raw.replace(",", "\n").replace(";", "\n").split("\n"):
        item = line.strip()
        if item:
            args += [flag, item]
    return args

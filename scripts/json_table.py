#!/usr/bin/env python3
"""Render the action's PR comment from a tidy JSON document.

Usage: json_table.py <rlt-run.json>

Prints Markdown findings and change records. Prints nothing for a
missing, unparseable, or empty document, so the caller can fall back to
its plain file list.
"""

import sys

from reporting import json_table


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: json_table.py <rlt-run.json>", file=sys.stderr)
        return 2
    json_table.main(argv[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())

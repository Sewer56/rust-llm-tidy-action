#!/usr/bin/env python3
"""Compare lint findings with the last published report.

The PR's current-findings comment stores a snapshot of its published findings.
This module compares that snapshot with a new scan:

- Identity: severity, code, path, message, item kind, and item name
- Location: changing only the line number does not count as a new finding.
- Presentation: changing only the title does not count as a new finding.
- Duplicates: two identical findings count twice; fixing one clears only one.
- Message changes: a changed message clears the old finding and adds a new one.

`merge_state` keeps unchanged findings linked to their first reported commit.
"""

import json
from collections import Counter

# Lint-finding severities the report tracks. Change records
# (`severity: "success"`) and unknown severities never enter comparisons;
# older binaries that emit only `error`/`warning` records are covered by
# the same filter.
FINDING_SEVERITIES = ("error", "warning", "hint", "reminder")

# Identity fields: everything a finding reports except its line (location
# moves are not changes) and its title (presentation only, absent from
# older records).
_IDENTITY_FIELDS = ("severity", "code", "path", "message", "item_kind", "item_name")


def load_records(path):
    """Records from a run's JSON document; `None` when unusable.

    A missing, unparseable or non-list document is a failed collection,
    never an empty successful result; callers must not publish state
    derived from `None`.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            records = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(records, list):
        return None
    return records


def findings(records):
    """The lint findings in a record list, input order preserved."""
    return [
        record
        for record in records
        if isinstance(record, dict) and record.get("severity") in FINDING_SEVERITIES
    ]


def identity(record):
    """Comparison identity of one finding record."""
    return tuple(str(record.get(field) or "") for field in _IDENTITY_FIELDS)


def entry(record, revision):
    """Stored snapshot entry: a finding plus the revision it describes."""
    return {"record": record, "revision": revision}


def merge_state(old_entries, records, revision):
    """Split findings into `(retained, added, cleared)`, preserving duplicate counts.

    Outputs:
    - `retained`: matching stored entries, with original records and revisions
    - `added`: new records paired with `revision`
    - `cleared`: stored entries with no remaining match
    """
    old_ids = Counter(identity(e["record"]) for e in old_entries)
    new_ids = Counter(identity(r) for r in records)

    # Keep as many copies of each finding as both scans contain.
    keep = old_ids & new_ids
    retained, cleared = [], []
    for stored in old_entries:
        key = identity(stored["record"])
        if keep.get(key):
            keep[key] -= 1
            retained.append(stored)
        else:
            cleared.append(stored)

    # Report only copies beyond the previously published count as additions.
    add_left = new_ids - old_ids
    added = []
    for record in records:
        key = identity(record)
        if add_left.get(key):
            add_left[key] -= 1
            added.append(entry(record, revision))
    return retained, added, cleared

#!/usr/bin/env python3
"""Deterministic comparison of published vs current lint findings.

The sticky PR report stores every finding it published; this module
decides what changed between that stored list and a fresh run's records:

- A finding's identity is its severity, code, path, message and item
  fields. The line number is not part of the identity, so a finding that
  only moved is unchanged and produces no report noise.
- Findings compare as multisets: duplicates keep their counts, so a
  second identical finding added is one added bullet, and one of a pair
  clearing is a single clearance.
- A record whose message or guidance changed has a new identity, so it
  reports as one cleared and one added finding.

`merge_state` splits stored entries and current records into retained
entries (kept with their original revision so their links stay pinned to
the commit they describe), added entries and cleared entries.
"""

import json
from collections import Counter

# Lint-finding severities the report tracks. Change records
# (`severity: "success"`) and unknown severities never enter comparisons;
# older binaries that emit only `error`/`warning` records are covered by
# the same filter.
FINDING_SEVERITIES = ("error", "warning", "hint")

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
    """Count-aware split into `(retained, added, cleared)` entries.

    Retained entries keep their original record and revision, so an
    unchanged finding never re-pins its link to the new commit. Added
    entries carry the current record and `revision`; cleared entries are
    the stored ones with no remaining counterpart.
    """
    old_ids = Counter(identity(e["record"]) for e in old_entries)
    new_ids = Counter(identity(r) for r in records)

    # Multiset intersection: per identity, the occurrences present in both.
    keep = old_ids & new_ids
    retained, cleared = [], []
    for stored in old_entries:
        key = identity(stored["record"])
        if keep.get(key):
            keep[key] -= 1
            retained.append(stored)
        else:
            cleared.append(stored)

    # Multiset difference: occurrences in `records` beyond the old counts.
    add_left = new_ids - old_ids
    added = []
    for record in records:
        key = identity(record)
        if add_left.get(key):
            add_left[key] -= 1
            added.append(entry(record, revision))
    return retained, added, cleared

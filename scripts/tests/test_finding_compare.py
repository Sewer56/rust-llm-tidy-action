"""Comparison behavior: identities, duplicates, location shifts."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reporting import finding_compare  # noqa: E402
from fake_gh import finding  # noqa: E402

OLD_REV = "a" * 40
NEW_REV = "b" * 40


def entries(*records):
    return [finding_compare.entry(record, OLD_REV) for record in records]


def merge(old_records, new_records):
    return finding_compare.merge_state(entries(*old_records), new_records, NEW_REV)


class LoadRecordsTests(unittest.TestCase):
    def test_load_records_returns_none_for_unusable_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            cases = {
                "missing": None,
                "garbage": "{not json",
                "object": '{"severity": "error"}',
            }
            for name, content in cases.items():
                with self.subTest(document=name):
                    path = Path(tmp) / f"{name}.json"
                    if content is not None:
                        path.write_text(content)
                    self.assertIsNone(finding_compare.load_records(path))
            valid = Path(tmp) / "list.json"
            valid.write_text(json.dumps([]))
            self.assertEqual(finding_compare.load_records(valid), [])

    def test_findings_keeps_only_lint_severities_in_input_order(self):
        records = [
            finding(severity="error"),
            {"severity": "success", "code": "REORDER", "path": "a.rs",
             "message": "m"},
            finding(severity="warning"),
            finding(severity="hint"),
            finding(severity="reminder"),
            {"severity": "catastrophe", "code": "X", "path": "a.rs",
             "message": "m"},
            "not even a dict",
        ]

        kept = finding_compare.findings(records)

        self.assertEqual(
            [record["severity"] for record in kept],
            ["error", "warning", "hint", "reminder"]
        )


class MergeTests(unittest.TestCase):
    def test_identical_findings_are_unchanged(self):
        current = [finding(), finding(severity="warning")]
        retained, added, cleared = merge(current, current)
        self.assertFalse(added)
        self.assertFalse(cleared)
        self.assertEqual(retained, entries(*current))

    def test_location_only_movement_is_not_a_change(self):
        retained, added, cleared = merge([finding(line=3)], [finding(line=99)])
        self.assertFalse(added)
        self.assertFalse(cleared)
        # The stored entry keeps the line and revision it was observed at.
        self.assertEqual(retained[0]["record"]["line"], 3)
        self.assertEqual(retained[0]["revision"], OLD_REV)

    def test_message_changes_report_as_cleared_and_added(self):
        old = [finding(message="old guidance")]
        new = [finding(message="new guidance")]
        retained, added, cleared = merge(old, new)
        self.assertFalse(retained)
        self.assertEqual([e["record"]["message"] for e in cleared], ["old guidance"])
        self.assertEqual([e["record"]["message"] for e in added], ["new guidance"])
        self.assertEqual(added[0]["revision"], NEW_REV)

    def test_duplicate_counts_are_preserved_in_both_directions(self):
        # Two identical findings drop to one: one retained, one cleared.
        retained, added, cleared = merge([finding(), finding()], [finding()])
        self.assertEqual((len(retained), len(added), len(cleared)), (1, 0, 1))

        # One grows to two: one retained, one added.
        retained, added, cleared = merge([finding()], [finding(), finding()])
        self.assertEqual((len(retained), len(added), len(cleared)), (1, 1, 0))

        # Duplicates at different lines are the same identity.
        retained, added, cleared = merge(
            [finding(line=1), finding(line=2)], [finding(line=9), finding(line=8)]
        )
        self.assertEqual((len(retained), len(added), len(cleared)), (2, 0, 0))

    def test_title_only_differences_are_not_changes(self):
        old = [finding(title="missing documentation")]
        new = [finding(title="updated documentation title")]
        retained, added, cleared = merge(old, new)
        self.assertEqual((len(retained), len(added), len(cleared)), (1, 0, 0))

    def test_empty_baseline_marks_everything_added_and_vice_versa(self):
        current = [finding(), finding(severity="hint")]
        retained, added, cleared = merge([], current)
        self.assertEqual((len(retained), len(added), len(cleared)), (0, 2, 0))
        self.assertEqual(
            [e["record"]["severity"] for e in added], ["error", "hint"]
        )

        retained, added, cleared = merge(current, [])
        self.assertEqual((len(retained), len(added), len(cleared)), (0, 0, 2))


if __name__ == "__main__":
    unittest.main()

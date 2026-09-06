"""Failure-comment rendering: hint sections, counts, bases, fallbacks."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json_table  # noqa: E402
from fake_gh import finding  # noqa: E402


def render(records):
    """stdout of json_table's CLI entry for a record list."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "run.json"
        path.write_text(json.dumps(records))
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            json_table.main(path)
        return buffer.getvalue()


class JsonTableTests(unittest.TestCase):
    def test_hints_render_as_a_separate_trailing_section(self):
        records = [
            finding(severity="warning", code="DOC008", path="src/lib.rs", line=9,
                    message="src/lib.rs: line 9 is 95 chars long; split it."),
            finding(severity="error", code="DOC002", path="src/main.rs", line=12,
                    message="pub fn returning Result is missing a `# Errors`"
                            " doc section", item_name="load"),
            finding(severity="hint", code="DOC999", path="src/app.rs", line=40,
                    message="consider pre-allocating the buffer"),
        ]
        out = render(records)
        self.assertEqual(out.splitlines()[0], "1 error, 1 warning, 1 hint.")
        self.assertIn("### Errors", out)
        self.assertIn("### Warnings", out)
        self.assertIn("### Hints - consider looking at these", out)
        # Hints keep the shared bullet shape and land last.
        self.assertLess(out.index("### Warnings"),
                        out.index("### Hints - consider looking at these"))
        self.assertIn("**`DOC999` missing documentation** - `src/app.rs:40`", out)
        self.assertIn("consider pre-allocating the buffer", out)

    def test_hint_only_document_renders_counts_and_section(self):
        records = [
            finding(severity="hint"),
            finding(severity="hint", code="DOC998", line=8),
        ]
        out = render(records)
        self.assertEqual(out.splitlines()[0], "2 hints.")
        self.assertNotIn("### Errors", out)
        self.assertNotIn("### Warnings", out)
        self.assertEqual(out.count("### Hints - consider looking at these"), 1)

    def test_legacy_records_render_without_optional_fields(self):
        records = [
            finding(item_kind=None, item_name=None, title=None, line=5,
                    message="missing doc comment"),
        ]
        out = render(records)
        # The TITLES map supplies the title older binaries never emitted.
        self.assertIn("**`DOC001` missing documentation** - `src/lib.rs:5`", out)
        self.assertIn("missing doc comment", out)

    def test_nothing_prints_for_unusable_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, content in {
                "missing": None,
                "garbage": "{oops",
                "empty-list": "[]",
            }.items():
                with self.subTest(document=name):
                    path = Path(tmp) / f"{name}.json"
                    if content is not None:
                        path.write_text(content)
                    buffer = io.StringIO()
                    with redirect_stdout(buffer):
                        json_table.main(path)
                    self.assertEqual(buffer.getvalue(), "")

    def test_location_uses_an_explicit_base_over_the_environment(self):
        os.environ["RLT_BLOB_BASE"] = "https://env.example/o/r/blob/envsha/"
        try:
            linked = json_table.location("src/lib.rs", 3,
                                         "https://direct.example/o/r/blob/dirsha/")
            self.assertEqual(
                linked,
                "[src/lib.rs:3](<https://direct.example/o/r/blob/dirsha/src/lib.rs#L3>)",
            )
            self.assertEqual(json_table.location("a.rs", None, ""),
                             "`a.rs`")
        finally:
            del os.environ["RLT_BLOB_BASE"]


if __name__ == "__main__":
    unittest.main()

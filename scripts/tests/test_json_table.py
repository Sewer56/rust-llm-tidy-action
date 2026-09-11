"""Failure-comment rendering: finding sections, counts, bases, fallbacks."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reporting import json_table  # noqa: E402
from fake_gh import finding  # noqa: E402

CLI_ENTRY = Path(__file__).resolve().parents[1] / "json_table.py"


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
    def test_reminders_should_render_separately_after_hints(self):
        records = [finding(severity="reminder", code="SYM001"),
                   finding(severity="hint")]

        out = render(records)

        self.assertEqual(out.splitlines()[0], "1 hint, 1 reminder.")
        self.assertLess(out.index("### Hints"), out.index("### Reminders"))
        self.assertIn("### Reminders - changed lines by default", out)
        self.assertIn("[`SYM001`]", out)

    def test_reminders_should_render_counts_and_locations_when_only_findings(self):
        records = [finding(severity="reminder", line=7),
                   finding(severity="reminder", line=9)]

        out = render(records)

        self.assertEqual(out.splitlines()[0], "2 reminders.")
        self.assertEqual(out.count("### Reminders"), 1)
        self.assertNotIn("### Hints", out)
        self.assertIn("src/lib.rs:7", out)
        self.assertIn("src/lib.rs:9", out)

    def test_ai_reminders_should_render_collapsed_after_reminders(self):
        records = [finding(severity="reminder", code="SYM001"),
                   finding(severity="ai_reminder", code="SYM002")]

        out = render(records)

        self.assertEqual(out.splitlines()[0], "1 reminder, 1 AI reminder.")
        self.assertLess(out.index("### Reminders - changed lines by default"),
                        out.index("<details>"))
        self.assertIn("<summary>Reminders for AI Language Models</summary>", out)
        self.assertNotIn("<details open>", out)
        # The AI bullet is the only content inside the one disclosure.
        self.assertEqual(out.count("<details>"), 1)
        self.assertEqual(out.count("</details>"), 1)
        self.assertLess(out.index("[`SYM002`]"), out.index("</details>"))
        self.assertEqual(out.splitlines()[-1], "</details>")

    def test_ai_reminders_should_render_counts_and_locations_when_only_findings(self):
        records = [finding(severity="ai_reminder", line=7),
                   finding(severity="ai_reminder", line=9)]

        out = render(records)

        self.assertEqual(out.splitlines()[0], "2 AI reminders.")
        self.assertNotIn("### Hints", out)
        self.assertIn("src/lib.rs:7", out)
        self.assertIn("src/lib.rs:9", out)

    def test_report_should_omit_the_disclosure_without_ai_reminders(self):
        out = render([finding(severity="reminder", code="SYM001")])

        self.assertNotIn("<details>", out)
        self.assertIn("### Reminders - changed lines by default", out)

    def test_ai_reminders_should_stay_before_the_changes_table(self):
        records = [
            {"severity": "success", "code": "FIX", "path": "src/lib.rs",
             "line": 1, "message": "fix"},
            finding(severity="reminder"),
            finding(severity="ai_reminder", code="SYM001"),
        ]

        out = render(records)

        self.assertEqual(out.splitlines()[0], "1 reminder, 1 AI reminder, 1 change.")
        self.assertLess(out.index("### Reminders"), out.index("<details>"))
        self.assertLess(out.index("</details>"), out.index("### Changes"))

    def test_ai_markup_in_message_or_title_cannot_restructure_the_report(self):
        records = [
            finding(severity="ai_reminder", code="SYM001", line=1,
                    title="Widgets",
                    message="Keep </details> then </DETAILS > and <details> open."),
            finding(severity="ai_reminder", code="SYM002", line=2,
                    title="Use <DeTaIlS open> here",
                    message='See <details data-x="a>b"> for the pattern.'),
            {"severity": "success", "code": "FIX", "path": "src/lib.rs",
             "line": 3, "message": "fix"},
        ]

        out = render(records)

        # Only the generated wrapper is markup; injected delimiters are text.
        self.assertEqual(out.count("<details"), 1)
        self.assertEqual(out.count("</details>"), 1)
        self.assertIn("&lt;/details>", out)
        self.assertIn("&lt;/DETAILS >", out)
        self.assertIn("&lt;details>", out)
        self.assertIn("&lt;DeTaIlS open>", out)
        self.assertIn('&lt;details data-x="a>b">', out)
        # Generated content after the section stays outside the disclosure.
        self.assertLess(out.index("</details>"), out.index("### Changes"))

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
        expected = (
            "**[`DOC999`](https://github.com/Sewer56/rust-llm-tidy/blob/main"
            "/docs/lints.md#codes) missing documentation** - `src/app.rs:40`"
        )
        self.assertIn(expected, out)
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

    def test_report_should_render_unnamed_findings_with_producer_titles(self):
        records = [
            finding(item_name=None, title="producer title", line=5,
                    message="missing doc comment"),
        ]

        out = render(records)

        self.assertIn(
            "**[`DOC001`](https://github.com/Sewer56/rust-llm-tidy/blob/main"
            "/docs/lints.md#codes) producer title** - `src/lib.rs:5`",
            out,
        )
        self.assertIn("missing doc comment", out)
        self.assertNotIn("`None`", out)

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

    def test_cli_entrypoint_should_print_the_rendered_report(self):
        # The preserved CLI contract: stdout equals the library render.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.json"
            path.write_text(json.dumps([finding()]))

            run = subprocess.run(
                [sys.executable, str(CLI_ENTRY), str(path)],
                capture_output=True, text=True,
            )

            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout, render([finding()]))

    def test_cli_entrypoint_should_demand_exactly_one_document_argument(self):
        cases = (("none", []), ("extra", ["a.json", "b.json"]))

        for name, argv in cases:
            with self.subTest(arguments=name):
                run = subprocess.run(
                    [sys.executable, str(CLI_ENTRY), *argv],
                    capture_output=True, text=True,
                )

                self.assertEqual(run.returncode, 2, run.stderr)
                self.assertIn("usage: json_table.py", run.stderr)


if __name__ == "__main__":
    unittest.main()

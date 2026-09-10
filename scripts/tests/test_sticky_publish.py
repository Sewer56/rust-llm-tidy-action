"""Sticky publication: state handling, authentication, budgets, recovery."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reporting import finding_compare  # noqa: E402
import sticky_publish  # noqa: E402
from fake_gh import (BOT, HEAD, NEW_HEAD, PR, REPOSITORY, SERVER, FakeTransport,
                     finding)  # noqa: E402

LOGINS = {BOT}
NEW2_HEAD = "d" * 40
MARKER = sticky_publish.MARKER


def publish_once(transport, records, revision=HEAD, **kwargs):
    return sticky_publish.publish(
        transport, REPOSITORY, PR, revision, records, SERVER, LOGINS, **kwargs
    )


def findings_doc(*records):
    return list(records)


def sticky_body(transport):
    """The newest owned sticky body in the fake PR conversation."""
    return transport.sticky_bodies()[-1]


class SnapshotCodecTests(unittest.TestCase):
    def test_snapshot_round_trips_through_the_hidden_comment(self):
        entries = [finding_compare.entry(finding(), HEAD)]
        state = {
            "schema": 1, "revision": HEAD, "overflow": False, "findings": entries,
        }
        line = sticky_publish.encode_state(state)
        self.assertTrue(line.startswith(MARKER))
        self.assertIn("-->", line)
        self.assertEqual(sticky_publish.decode_state(line), state)

    def test_decode_rejects_invalid_states(self):
        valid = sticky_publish.encode_state(
            {"schema": 1, "revision": HEAD, "overflow": False, "findings": []}
        )
        broken = {
            "no marker": "just a comment",
            "unterminated": valid[: -len(" -->")],
            "not base64": f"{MARKER}!!! -->",
            "spoofed marker first": f"junk {MARKER}AAAA --> middle {valid}",
        }
        # The appended snapshot is the last marker occurrence, so lookalike
        # text in bullets cannot shadow it.
        self.assertEqual(
            sticky_publish.decode_state(broken["spoofed marker first"]),
            sticky_publish.decode_state(valid),
        )
        del broken["spoofed marker first"]
        for name, body in broken.items():
            with self.subTest(state=name):
                self.assertIsNone(sticky_publish.decode_state(body))
        for name, state in {
            "wrong schema": {"schema": 2, "revision": HEAD, "overflow": False,
                             "findings": []},
            "non-string revision": {"schema": 1, "revision": 5, "overflow": False,
                                    "findings": []},
            "bad entries": {"schema": 1, "revision": HEAD, "overflow": False,
                            "findings": [{"record": "nope", "revision": HEAD}]},
        }.items():
            with self.subTest(state=name):
                self.assertIsNone(
                    sticky_publish.decode_state(sticky_publish.encode_state(state))
                )


class PublishTests(unittest.TestCase):
    def test_sticky_should_show_raw_messages_for_multiple_findings(self):
        transport = FakeTransport()
        warning = finding(severity="warning", message="src/lib.rs: First. Second;")
        reminder = finding(severity="reminder", message="Keep lines:\n```\n<b>literal</b>")

        publish_once(transport, [warning, reminder])

        body = sticky_body(transport)
        self.assertIn("      src/lib.rs: First. Second;\n", body)
        self.assertIn("      Keep lines:\n      ```\n      <b>literal</b>\n", body)
        self.assertEqual(body.count(sticky_publish.REMINDER_NOTE), 1)
        self.assertEqual(len(sticky_publish.decode_state(body)["findings"]), 2)

    def test_sticky_should_omit_reminder_note_without_reminders(self):
        transport = FakeTransport()

        publish_once(transport, [finding()])

        self.assertNotIn(sticky_publish.REMINDER_NOTE, sticky_body(transport))

    def test_reminders_should_retain_then_clear_their_original_revision(self):
        transport = FakeTransport()
        reminder = finding(severity="reminder", code="SYM001", line=7)

        publish_once(transport, [reminder])

        body = sticky_body(transport)
        self.assertIn("1 reminder.", body)
        self.assertIn("### Reminders\n", body)
        self.assertEqual(sticky_publish.decode_state(body)["findings"],
                         [finding_compare.entry(reminder, HEAD)])

        transport.head_sha = NEW_HEAD
        summary = publish_once(transport, [dict(reminder, line=9)], revision=NEW_HEAD)

        self.assertEqual(summary["status"], "unchanged")
        self.assertEqual(len(transport.writes()), 1)

        summary = publish_once(transport, [], revision=NEW_HEAD)

        self.assertTrue(summary["delta_posted"])
        self.assertIn("No current findings.", sticky_body(transport))
        delta = transport.calls[-1][2]["body"]
        self.assertIn("0 added, 1 cleared", delta)
        self.assertIn(f"blob/{HEAD}/src/lib.rs#L7", delta)

    def test_reminders_should_render_after_hints_when_added(self):
        transport = FakeTransport()
        hint = finding(severity="hint")
        publish_once(transport, [hint])
        transport.head_sha = NEW_HEAD
        reminder = finding(severity="reminder", code="SYM001")

        summary = publish_once(transport, [hint, reminder], revision=NEW_HEAD)

        self.assertTrue(summary["delta_posted"])
        body = sticky_body(transport)
        self.assertIn("1 hint, 1 reminder.", body)
        self.assertLess(body.index("### Hints"), body.index("### Reminders"))
        self.assertIn("1 added, 0 cleared", transport.calls[-1][2]["body"])

    def test_reminders_should_respect_report_budget_when_snapshot_overflows(self):
        transport = FakeTransport()
        records = [finding(severity="reminder", path=f"src/f{i}.rs")
                   for i in range(5000)]

        publish_once(transport, records)

        body = sticky_body(transport)
        self.assertLessEqual(len(body), sticky_publish.MAX_COMMENT_CHARS)
        self.assertIn("5000 reminders.", body)
        self.assertIn("### Reminders\n", body)
        self.assertIn("more not shown", body)
        self.assertTrue(sticky_publish.decode_state(body)["overflow"])

    def test_first_findings_create_one_sticky_without_a_change_report(self):
        transport = FakeTransport()
        summary = publish_once(transport, findings_doc(finding()))
        self.assertEqual(summary["status"], "created")
        self.assertTrue(summary["sticky_url"])
        writes = transport.writes()
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0], ("POST", f"repos/{REPOSITORY}/issues/{PR}/comments"))
        body = sticky_body(transport)
        self.assertIn("## rust-llm-tidy: current findings", body)
        self.assertIn("1 error.", body)
        self.assertIn("### Errors", body)
        state = sticky_publish.decode_state(body)
        self.assertEqual(len(state["findings"]), 1)

    def test_unchanged_rerun_writes_nothing(self):
        transport = FakeTransport()
        publish_once(transport, findings_doc(finding()))
        self.assertEqual(publish_once(transport, findings_doc(finding())),
                         {
                             "status": "unchanged", "sticky_url": None,
                             "delta_posted": False, "delta_failed": False,
                             "deleted_extra": 0, "reason": None,
                         })
        self.assertEqual(transport.writes(), [
            ("POST", f"repos/{REPOSITORY}/issues/{PR}/comments")
        ])

    def test_location_only_movement_writes_nothing(self):
        transport = FakeTransport()
        publish_once(transport, findings_doc(finding(line=3)))
        transport.head_sha = NEW_HEAD
        summary = publish_once(
            transport, findings_doc(finding(line=4242)), revision=NEW_HEAD
        )
        self.assertEqual(summary["status"], "unchanged")
        self.assertEqual(len(transport.writes()), 1)  # only the initial create

    def test_added_and_cleared_links_pin_their_own_revisions(self):
        base = finding(code="DOC001", path="src/lib.rs", line=3)
        extra = finding(severity="warning", code="DOC008", path="src/main.rs",
                        line=9, message="line 9 is 95 chars long.")
        transport = FakeTransport()
        publish_once(transport, findings_doc(base))
        transport.head_sha = NEW_HEAD
        summary = publish_once(transport, findings_doc(base, extra), revision=NEW_HEAD)
        self.assertEqual(summary["status"], "updated")
        self.assertTrue(summary["delta_posted"])

        # Retained findings keep the revision they were observed at; added
        # ones pin the new revision.
        body = sticky_body(transport)
        self.assertIn(f"blob/{HEAD}/src/lib.rs#L3", body)
        self.assertIn(f"blob/{NEW_HEAD}/src/main.rs#L9", body)

        delta = transport.calls[-1][2]["body"]
        self.assertIn("## rust-llm-tidy: findings changed", delta)
        self.assertIn("1 added, 0 cleared", delta)
        self.assertIn(f"blob/{NEW_HEAD}/src/main.rs#L9", delta)
        self.assertIn("[full report](", delta)

        # Clearing the base finding reports it at its stored revision.
        transport.head_sha = NEW2_HEAD
        summary = publish_once(transport, findings_doc(extra), revision=NEW2_HEAD)
        self.assertEqual(summary["status"], "updated")
        delta = transport.calls[-1][2]["body"]
        self.assertIn("0 added, 1 cleared", delta)
        self.assertIn(f"blob/{HEAD}/src/lib.rs#L3", delta)

    def test_last_finding_clear_leaves_an_empty_retained_sticky(self):
        transport = FakeTransport()
        publish_once(transport, findings_doc(finding()))
        transport.head_sha = NEW_HEAD
        summary = publish_once(transport, [], revision=NEW_HEAD)
        self.assertEqual(summary["status"], "updated")
        self.assertTrue(summary["delta_posted"])
        body = sticky_body(transport)
        self.assertIn("No current findings.", body)
        state = sticky_publish.decode_state(body)
        self.assertEqual(state["findings"], [])
        self.assertEqual(state["revision"], NEW_HEAD)
        # The sticky is updated, never deleted.
        self.assertNotIn("DELETE", [method for method, _ in transport.writes()])

    def test_hints_render_in_their_own_section(self):
        transport = FakeTransport()
        publish_once(
            transport,
            findings_doc(finding(), finding(severity="hint", code="DOC999")),
        )
        body = sticky_body(transport)
        self.assertIn("1 error, 1 hint.", body)
        self.assertIn("### Errors", body)
        self.assertIn("### Hints - consider looking at these", body)
        self.assertLess(body.index("### Errors"),
                        body.index("### Hints - consider looking at these"))

    def test_no_sticky_is_created_for_an_empty_run_without_baseline(self):
        transport = FakeTransport()
        summary = publish_once(transport, [])
        self.assertEqual(summary["status"], "noop")
        self.assertEqual(transport.writes(), [])

    def test_marker_comments_from_other_users_are_not_owned(self):
        transport = FakeTransport()
        spoof = transport.comment(author="attacker", body=f"junk {MARKER}AAAA -->")
        transport.comments.append(spoof)
        publish_once(transport, findings_doc(finding()))
        # A fresh owned sticky is created; the spoof is neither adopted nor
        # rewritten.
        self.assertEqual(
            transport.writes(),
            [("POST", f"repos/{REPOSITORY}/issues/{PR}/comments")],
        )
        self.assertEqual(
            [c for c in transport.comments if c["id"] == spoof["id"]][0]["body"],
            spoof["body"],
        )

    def test_deleted_sticky_is_recreated_without_invented_history(self):
        transport = FakeTransport()
        publish_once(transport, findings_doc(finding()))
        transport.comments = []  # someone deleted the sticky
        transport.head_sha = NEW_HEAD
        summary = publish_once(
            transport, findings_doc(finding(line=8)), revision=NEW_HEAD
        )
        self.assertEqual(summary["status"], "created")
        self.assertFalse(summary["delta_posted"])

    def test_corrupt_snapshot_rebaselines_in_place(self):
        transport = FakeTransport()
        owned = transport.comment(
            body="## rust-llm-tidy: current findings\n1 error.\n"
                 f"{MARKER}%%% not base64 -->"
        )
        transport.comments.append(owned)
        summary = publish_once(transport, findings_doc(finding()))
        self.assertEqual(summary["status"], "updated")
        self.assertFalse(summary["delta_posted"])
        # The repaired body now carries a decodable snapshot.
        state = sticky_publish.decode_state(owned["body"])
        self.assertEqual(len(state["findings"]), 1)

    def test_overflow_bounds_the_payload_and_never_reports_false_clears(self):
        transport = FakeTransport()
        many = [finding(path=f"src/f{i:05}.rs", line=i + 1) for i in range(5000)]
        publish_once(transport, many)
        body = sticky_body(transport)
        self.assertLessEqual(len(body), sticky_publish.MAX_COMMENT_CHARS)
        state = sticky_publish.decode_state(body)
        self.assertTrue(state["overflow"])
        self.assertIn("Report budget exceeded", body)
        self.assertIn("5000 errors.", body)
        self.assertIn("not shown (report size budget)", body)

        # A rerun against the overflowed state re-baselines quietly.
        transport.head_sha = NEW_HEAD
        summary = publish_once(transport, many, revision=NEW_HEAD)
        self.assertEqual(summary["status"], "updated")
        self.assertFalse(summary["delta_posted"])

    def test_overflow_recovery_rebaselines_without_a_cleared_flood(self):
        transport = FakeTransport()
        many = [finding(path=f"src/f{i:05}.rs") for i in range(5000)]
        publish_once(transport, many)
        transport.head_sha = NEW_HEAD
        few = [finding(), finding(severity="warning")]
        summary = publish_once(transport, few, revision=NEW_HEAD)
        self.assertEqual(summary["status"], "updated")
        self.assertFalse(summary["delta_posted"])
        body = sticky_body(transport)
        self.assertIn("1 error, 1 warning.", body)
        self.assertFalse(sticky_publish.decode_state(body)["overflow"])

    def test_display_truncation_keeps_the_full_snapshot(self):
        # Mid-size: the bullets exceed the budget but the snapshot still
        # fits, so only the display truncates (no overflow declaration).
        records = [finding(path=f"src/f{i:05}.rs", line=i + 1, message="x" * 40)
                   for i in range(130)]
        transport = FakeTransport()
        publish_once(transport, records)
        body = sticky_body(transport)
        self.assertLessEqual(len(body), sticky_publish.MAX_COMMENT_CHARS)
        state = sticky_publish.decode_state(body)
        self.assertFalse(state["overflow"])
        self.assertEqual(len(state["findings"]), 130)
        self.assertIn("not shown (report size budget)", body)

        # Every finding is still stored, so the same findings at a new
        # head read as unchanged: display truncation never clears.
        transport.head_sha = NEW_HEAD
        summary = publish_once(transport, records, revision=NEW_HEAD)
        self.assertEqual(summary["status"], "unchanged")

    def test_stale_run_never_overwrites_newer_state(self):
        transport = FakeTransport(head_sha=NEW_HEAD)
        summary = publish_once(transport, findings_doc(finding()), revision=HEAD)
        self.assertEqual(summary["status"], "stale")
        self.assertEqual(transport.writes(), [])

    def test_head_moving_during_publication_writes_nothing(self):
        transport = FakeTransport()
        publish_once(transport, findings_doc(finding()))
        writes_before = len(transport.writes())
        # This run describes NEW_HEAD, so the entry gate passes; the PR
        # then advances during the comment scan and the pre-write
        # re-check must catch it.
        transport.head_sha = NEW_HEAD
        original = transport.request

        def moving_head(method, path, body=None):
            if method == "GET" and "/comments?" in path:  # during the scan
                transport.head_sha = NEW2_HEAD
            return original(method, path, body)

        transport.request = moving_head
        summary = publish_once(
            transport,
            findings_doc(finding(), finding(severity="warning")),
            revision=NEW_HEAD,
        )
        self.assertEqual(summary["status"], "stale")
        self.assertEqual(len(transport.writes()), writes_before)

    def test_missing_findings_document_fails_closed(self):
        transport = FakeTransport()
        summary = publish_once(transport, None)
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(transport.calls, [])

    def test_denied_comment_permissions_produce_no_writes(self):
        transport = FakeTransport()
        transport.fail("GET", f"issues/{PR}/comments", 403)
        summary = publish_once(transport, findings_doc(finding()))
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(transport.writes(), [])

        transport = FakeTransport()
        transport.fail("POST", f"issues/{PR}/comments", 403)
        summary = publish_once(transport, findings_doc(finding()))
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(transport.writes(), [("POST",
                                               f"repos/{REPOSITORY}/issues/{PR}/comments")])

    def test_failed_change_report_keeps_the_updated_sticky(self):
        transport = FakeTransport()
        publish_once(transport, findings_doc(finding()))
        transport.fail("POST", f"issues/{PR}/comments", 403)
        transport.head_sha = NEW_HEAD
        summary = publish_once(
            transport, findings_doc(finding(), finding(severity="warning")),
            revision=NEW_HEAD,
        )
        self.assertEqual(summary["status"], "updated")
        self.assertTrue(summary["delta_failed"])
        self.assertFalse(summary["delta_posted"])
        # The sticky itself still carries the new state.
        self.assertIn("1 error, 1 warning.", sticky_body(transport))

    def test_concurrent_duplicates_consolidate_into_one_sticky(self):
        transport = FakeTransport()
        publish_once(transport, findings_doc(finding()))
        # A second run created its own sticky before seeing the first.
        race = transport.comment(body=sticky_body(transport))
        race["id"] = transport.next_id
        transport.next_id += 1
        transport.comments.append(race)
        transport.head_sha = NEW_HEAD
        summary = publish_once(
            transport, findings_doc(finding(line=2)), revision=NEW_HEAD
        )
        self.assertEqual(summary["status"], "updated")
        self.assertEqual(summary["deleted_extra"], 1)
        self.assertEqual(len(transport.sticky_bodies()), 1)

    def test_authenticated_token_identity_matches_its_own_stickies(self):
        transport = FakeTransport(login="ci-bot")
        publish_once(transport, findings_doc(finding()))
        transport.head_sha = NEW_HEAD
        # The sticky's author is the token identity, not the configured
        # default login; it must still count as owned.
        summary = publish_once(
            transport, findings_doc(finding(line=9)), revision=NEW_HEAD
        )
        self.assertEqual(summary["status"], "unchanged")
        self.assertEqual(len(transport.writes()), 1)

    def test_malicious_finding_text_stays_inside_the_snapshot(self):
        hostile = finding(
            path="src/ev]il[.rs",
            message="see [--](x) and --> and ]] and "
                    f"{MARKER}AAAA --> and [link](https://evil.example)",
        )
        transport = FakeTransport()
        publish_once(transport, findings_doc(hostile, finding()))
        body = sticky_body(transport)
        state = sticky_publish.decode_state(body)
        stored = [e["record"] for e in state["findings"]]
        self.assertEqual(
            [r["path"] for r in stored], ["src/ev]il[.rs", finding()["path"]]
        )
        # The snapshot stays the final word: hostile text earlier in the
        # body cannot shadow or close it.
        self.assertTrue(body.endswith(sticky_publish.encode_state(state)))
        self.assertIn("ev\\]il\\[.rs", body)

    def test_comment_scanning_is_paginated_and_bounded(self):
        transport = FakeTransport()
        transport.comments = [transport.comment(body=f"comment {i}")
                              for i in range(250)]
        publish_once(transport, findings_doc(finding()))
        listing = [path for method, path, _ in transport.calls
                   if "issues/comments?" in path or "/comments?" in path]
        self.assertEqual(len(listing), 3)  # 100 + 100 + 50 (short page stops)

        bounded = FakeTransport()
        bounded.comments = [bounded.comment(body=f"comment {i}")
                            for i in range(250)]
        summary = publish_once(bounded, findings_doc(finding()), max_pages=2)
        self.assertEqual(summary["status"], "created")


class DeltaRenderingTests(unittest.TestCase):
    def test_quiet_changes_render_no_comment(self):
        self.assertIsNone(sticky_publish.render_delta([], [], "url", HEAD,
                                                      SERVER, REPOSITORY))

    def test_large_delta_truncates_within_the_budget(self):
        added = [{"record": finding(path=f"src/f{i:05}.rs", line=i + 1,
                                    message="m" * 40), "revision": HEAD}
                 for i in range(5000)]
        delta = sticky_publish.render_delta(added, [], "url", HEAD, SERVER,
                                            REPOSITORY)
        self.assertLessEqual(len(delta), sticky_publish.MAX_COMMENT_CHARS)
        self.assertIn("not shown (report size budget)", delta)
        self.assertIn("5000 added, 0 cleared", delta)


if __name__ == "__main__":
    unittest.main()

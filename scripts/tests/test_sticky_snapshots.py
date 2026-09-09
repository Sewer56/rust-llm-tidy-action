"""Sticky publication against snapshots from earlier action versions."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reporting import finding_compare  # noqa: E402
import sticky_publish  # noqa: E402
from fake_gh import (HEAD, NEW_HEAD, FakeTransport, finding)  # noqa: E402
from test_sticky_publish import (NEW2_HEAD, publish_once,  # noqa: E402
                                 sticky_body)


def seeded_transport(stored):
    """A PR whose sticky snapshot already holds `stored` findings."""
    transport = FakeTransport(head_sha=NEW_HEAD)
    state = {
        "schema": sticky_publish.SCHEMA_VERSION,
        "revision": HEAD,
        "overflow": False,
        "findings": [finding_compare.entry(record, HEAD) for record in stored],
    }
    transport.comments.append(
        transport.comment(body=sticky_publish.encode_state(state))
    )
    return transport


def legacy_finding():
    """A finding as stored before the CLI emitted titles and item fields."""
    record = finding()
    for field in ("title", "item_kind", "item_name"):
        del record[field]
    return record


class OlderSnapshotTests(unittest.TestCase):
    def test_publication_should_rebaseline_older_snapshots_with_raw_code_titles(self):
        transport = seeded_transport([legacy_finding()])
        current = finding()
        extra = finding(path="src/new.rs")

        summary = publish_once(transport, [current, extra], revision=NEW_HEAD)

        # The older record lacks identity fields, so it clears and the
        # current scan re-adds it; the cleared bullet renders the raw
        # code, since no title fallback exists anymore.
        self.assertTrue(summary["delta_posted"])
        delta = transport.calls[-1][2]["body"]
        self.assertIn("2 added, 1 cleared", delta)
        self.assertIn("#codes) DOC001**", delta)
        self.assertIn(f"blob/{HEAD}/src/lib.rs#L3", delta)
        body = sticky_body(transport)
        self.assertIn("missing documentation**", body)
        state = sticky_publish.decode_state(body)
        self.assertEqual(
            [e["record"] for e in state["findings"]], [current, extra]
        )

        transport.head_sha = NEW2_HEAD
        summary = publish_once(transport, [extra], revision=NEW2_HEAD)

        self.assertTrue(summary["delta_posted"])
        self.assertNotIn("src/lib.rs", sticky_body(transport))
        delta = transport.calls[-1][2]["body"]
        self.assertIn("0 added, 1 cleared", delta)
        self.assertIn(f"blob/{NEW_HEAD}/src/lib.rs#L3", delta)


if __name__ == "__main__":
    unittest.main()

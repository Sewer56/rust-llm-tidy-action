"""`gh api` transport: request shape, parsing, method-aware retries."""

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reporting import gh_api  # noqa: E402

# Stub `gh` binaries: each records its argv to a log and answers per its
# script. `{log}` and `{stdin}` are substituted by the installing test.
_STUB_OK = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "{log}"
cat > "{stdin}"
printf '{{"ok": true}}\\n'
"""

_STUB_EMPTY = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "{log}"
exit 0
"""

# Fails with `HTTP <status>` the first N calls, then answers plainly.
_STUB_FLAKY = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "{log}"
count="$(cat '{state}' 2>/dev/null || printf 0)"
count=$((count + 1))
printf '%s' "$count" > '{state}'
if [ "$count" -le {fails} ]; then
  printf 'gh: Server Error (HTTP {status})\\n' >&2
  exit 1
fi
printf '{{"ok": true}}\\n'
"""

# Fails with no `HTTP <status>` diagnostic, like a network-level error.
_STUB_NETERR = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "{log}"
printf 'gh: connection reset by peer\\n' >&2
exit 1
"""

# Sleeps past the (test-shortened) request timeout on every call. The
# early fd close lets `subprocess.run` see EOF once the stub is killed.
_STUB_SLOW = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "{log}"
exec 1>&- 2>&-
sleep 2
"""


def install_gh(directory, script, **fields):
    """Write an executable `gh` stub into `directory`; returns its log path."""
    log = directory / "gh.log"
    stub = directory / "gh"
    stub.write_text(script.format(
        log=log, stdin=directory / "stdin", state=directory / "state", **fields
    ))
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return log


def invocation_count(log):
    """Number of `gh` invocations recorded in the stub log so far."""
    if not log.exists():
        return 0
    return len(log.read_text().strip().splitlines())


class GhApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.bin = Path(self._tmp.name)
        self._path = os.environ.get("PATH")
        os.environ["PATH"] = f"{self.bin}:{self._path}"

    def tearDown(self):
        if self._path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = self._path
        self._tmp.cleanup()

    def shorten_timeout(self, seconds):
        """Shrink the per-request timeout so a sleeping stub trips it."""
        original = gh_api._TIMEOUT_SECONDS
        gh_api._TIMEOUT_SECONDS = seconds
        self.addCleanup(setattr, gh_api, "_TIMEOUT_SECONDS", original)

    def test_sends_method_path_and_json_body(self):
        log = install_gh(self.bin, _STUB_OK)
        api = gh_api.GhApi()
        result = api.request(
            "post", "repos/o/r/issues/7/comments", {"body": "hello"}
        )
        self.assertEqual(result, {"ok": True})
        argv = log.read_text().strip().splitlines()[-1]
        self.assertIn("--method POST", argv)
        self.assertIn("repos/o/r/issues/7/comments", argv)
        self.assertIn("--input -", argv)
        self.assertIn("Content-Type: application/json", argv)
        self.assertEqual(
            json.loads((self.bin / "stdin").read_text()), {"body": "hello"}
        )

    def test_empty_response_bodies_parse_as_none(self):
        log = install_gh(self.bin, _STUB_EMPTY)
        api = gh_api.GhApi()
        self.assertIsNone(api.request("DELETE", "repos/o/r/issues/comments/1"))
        self.assertTrue(log.read_text().strip())

    def test_retries_server_errors_then_succeeds(self):
        log = install_gh(self.bin, _STUB_FLAKY, fails=2, status=502)
        api = gh_api.GhApi(attempts=3, backoff=())
        self.assertEqual(api.request("GET", "repos/o/r/pulls/7"), {"ok": True})
        self.assertEqual(len(log.read_text().strip().splitlines()), 3)

    def test_retries_stop_after_bounded_attempts(self):
        log = install_gh(self.bin, _STUB_FLAKY, fails=99, status=500)
        api = gh_api.GhApi(attempts=3, backoff=())
        with self.assertRaises(gh_api.TransportError) as raised:
            api.request("GET", "repos/o/r/pulls/7")
        self.assertEqual(raised.exception.status, 500)
        self.assertEqual(len(log.read_text().strip().splitlines()), 3)

    def test_client_errors_fail_without_retry(self):
        log = install_gh(self.bin, _STUB_FLAKY, fails=99, status=404)
        api = gh_api.GhApi(attempts=3, backoff=())
        with self.assertRaises(gh_api.TransportError) as raised:
            api.request("GET", "repos/o/r/pulls/7")
        self.assertEqual(raised.exception.status, 404)
        self.assertEqual(len(log.read_text().strip().splitlines()), 1)

    def test_non_idempotent_writes_fail_fast_on_server_errors(self):
        log = install_gh(self.bin, _STUB_FLAKY, fails=99, status=500)
        api = gh_api.GhApi(attempts=3, backoff=())
        for method in ("POST", "PATCH"):
            with self.subTest(method=method):
                before = invocation_count(log)

                with self.assertRaises(gh_api.TransportError) as raised:
                    api.request(method, "repos/o/r/issues/7/comments",
                                {"body": "hello"})

                self.assertEqual(raised.exception.status, 500)
                # Fail fast: exactly one `gh` invocation, no replay.
                self.assertEqual(invocation_count(log), before + 1)

    def test_non_idempotent_writes_fail_fast_on_status_less_failures(self):
        log = install_gh(self.bin, _STUB_NETERR)
        api = gh_api.GhApi(attempts=3, backoff=())
        for method in ("POST", "PATCH"):
            with self.subTest(method=method):
                before = invocation_count(log)

                with self.assertRaises(gh_api.TransportError) as raised:
                    api.request(method, "repos/o/r/issues/7/comments",
                                {"body": "hello"})

                self.assertIsNone(raised.exception.status)
                # Fail fast: exactly one `gh` invocation, no replay.
                self.assertEqual(invocation_count(log), before + 1)

    def test_non_idempotent_writes_fail_fast_on_timeouts(self):
        log = install_gh(self.bin, _STUB_SLOW)
        api = gh_api.GhApi(attempts=3, backoff=())
        self.shorten_timeout(0.2)
        for method in ("POST", "PATCH"):
            with self.subTest(method=method):
                before = invocation_count(log)

                with self.assertRaises(gh_api.TransportError) as raised:
                    api.request(method, "repos/o/r/issues/7/comments",
                                {"body": "hello"})

                self.assertIsNone(raised.exception.status)
                self.assertIn("timed out", str(raised.exception))
                # Fail fast: exactly one `gh` invocation, no replay.
                self.assertEqual(invocation_count(log), before + 1)

    def test_timeouts_still_retry_idempotent_requests(self):
        log = install_gh(self.bin, _STUB_SLOW)
        api = gh_api.GhApi(attempts=3, backoff=())
        self.shorten_timeout(0.2)

        with self.assertRaises(gh_api.TransportError) as raised:
            api.request("GET", "repos/o/r/pulls/7")

        self.assertIn("timed out", str(raised.exception))
        self.assertEqual(invocation_count(log), 3)

    def test_missing_gh_binary_fails_as_a_transport_error(self):
        # An empty stub directory: `gh` is not on PATH at all.
        os.environ["PATH"] = str(self.bin)
        api = gh_api.GhApi(attempts=3, backoff=())
        with self.assertRaises(gh_api.TransportError) as raised:
            api.request("GET", "user")
        self.assertIsNone(raised.exception.status)
        self.assertIn("could not run gh", str(raised.exception))


if __name__ == "__main__":
    unittest.main()

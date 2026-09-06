"""Shared fixtures for the action's offline script tests.

`FakeTransport` is an in-memory stand-in for `gh_api.GhApi`: it serves the
small REST surface `sticky_publish` uses.

- Requests are recorded as `(method, path, body)` in `.calls`.
- Failures are scripted as status lists per `(method, substring)`.

`finding()` builds records in the CLI's JSON shape.
Omit optional fields to simulate records from older binaries.
"""

import re
import sys
from pathlib import Path
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gh_api import TransportError  # noqa: E402

SERVER = "https://github.test"
REPOSITORY = "owner/repo"
PR = 7
BOT = "github-actions[bot]"
HEAD = "f" * 40
NEW_HEAD = "e" * 40


def finding(severity="error", code="DOC001", path="src/lib.rs", line=3,
            message="non-private item is missing a doc comment",
            item_kind="fn", item_name="hello", title="missing documentation"):
    """One lint-finding record in the CLI's JSON document shape."""
    record = {
        "severity": severity,
        "code": code,
        "path": path,
        "line": line,
        "message": message,
    }
    if item_kind is not None:
        record["item_kind"] = item_kind
    if item_name is not None:
        record["item_name"] = item_name
    if title is not None:
        record["title"] = title
    return record


class FakeTransport:
    """In-memory GitHub PR conversation serving `sticky_publish` requests."""

    def __init__(self, head_sha=HEAD, login=BOT, comments=None):
        self.head_sha = head_sha
        self.login = login
        self.comments = list(comments or [])
        self.next_id = 1000 + len(self.comments)
        self.calls = []
        self.failures = {}

    def fail(self, method, substring, *statuses):
        """Script HTTP failures for requests whose path contains `substring`."""
        self.failures[(method.upper(), substring)] = list(statuses)

    def comment(self, author=BOT, body="unrelated"):
        """A pre-existing PR comment (owned stickies are usually built by
        publishing instead)."""
        return {
            "id": self.next_id,
            "user": {"login": author},
            "body": body,
            "html_url": f"{SERVER}/{REPOSITORY}/pull/{PR}#issuecomment-{self.next_id}",
        }

    def _maybe_fail(self, method, path):
        for (fail_method, substring), statuses in list(self.failures.items()):
            if fail_method == method and substring in path and statuses:
                status = statuses.pop(0)
                if not statuses:
                    del self.failures[(fail_method, substring)]
                raise TransportError(status, f"gh: simulated (HTTP {status})")

    def request(self, method, path, body=None):
        method = method.upper()
        self.calls.append((method, path, body))
        self._maybe_fail(method, path)

        pr_comments = re.fullmatch(
            rf"repos/{REPOSITORY}/issues/{PR}/comments(?:\?(.*))?", path
        )
        if method == "GET" and path == "user":
            return {"login": self.login}
        if method == "GET" and re.fullmatch(rf"repos/{REPOSITORY}/pulls/{PR}", path):
            return {"number": PR, "head": {"sha": self.head_sha}}
        if method == "GET" and pr_comments:
            query = parse_qs(pr_comments.group(1) or "")
            page = int(query.get("page", ["1"])[0])
            per_page = int(query.get("per_page", ["30"])[0])
            start = (page - 1) * per_page
            return [dict(comment) for comment in self.comments[start:start + per_page]]

        by_id = re.fullmatch(rf"repos/{REPOSITORY}/issues/comments/(\d+)", path)
        if method == "PATCH" and by_id:
            for comment in self.comments:
                if comment["id"] == int(by_id.group(1)):
                    comment["body"] = body["body"]
                    return dict(comment)
            raise TransportError(404, "gh: Not Found (HTTP 404)")
        if method == "POST" and re.fullmatch(
            rf"repos/{REPOSITORY}/issues/{PR}/comments", path
        ):
            comment = self.comment(body=body["body"])
            self.next_id += 1
            self.comments.append(comment)
            return dict(comment)
        if method == "DELETE" and by_id:
            self.comments = [
                comment
                for comment in self.comments
                if comment["id"] != int(by_id.group(1))
            ]
            return None
        raise AssertionError(f"unexpected request: {method} {path}")

    # Convenience views over the recorded calls.
    def writes(self):
        return [(method, path) for method, path, _ in self.calls
                if method in ("POST", "PATCH", "DELETE")]

    def sticky_bodies(self):
        return [
            comment["body"]
            for comment in self.comments
            if "<!-- rlt-sticky:v1:" in comment["body"]
        ]

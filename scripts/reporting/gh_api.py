#!/usr/bin/env python3
"""Authenticated GitHub REST transport on top of the `gh` CLI.

The runner's `gh` carries the token (via `GH_TOKEN`/`GITHUB_TOKEN`), so
this module never handles credentials itself; it only shapes requests
and parses responses. One `request` call is one HTTP request:

- Idempotent requests (GET, HEAD, PUT, DELETE) retry server errors
  (HTTP 5xx), network/CLI failures and timeouts a bounded number of
  times with short fixed backoff.
- Non-idempotent writes (POST, PATCH) fail fast on those same
  uncertain failures: the server may already have applied the write,
  so a replay could duplicate it.
- Client errors (HTTP 4xx) fail immediately; retrying them cannot help.
- A 2xx response with an empty body (e.g. a 204 after DELETE) parses as
  `None`.

Tests inject a fake object exposing the same `request(method, path,
body)` method instead of shelling out.
"""

import json
import re
import subprocess
import time

# `gh: Not Found (HTTP 404)` style diagnostics carry the only status
# information available without extra flags.
_HTTP_STATUS = re.compile(r"HTTP (\d{3})")

# One request timeout so a hung CLI cannot stall the reporting step.
_TIMEOUT_SECONDS = 60

# Methods safe to replay after a failure with an uncertain outcome; a
# replayed POST/PATCH could duplicate a write the server applied.
_IDEMPOTENT = frozenset({"GET", "HEAD", "PUT", "DELETE"})


class TransportError(RuntimeError):
    """One GitHub request failed after its bounded retries.

    `status` is the HTTP status code when the server answered, else
    `None` (network or CLI-level failure). `message` carries the CLI's
    stderr diagnostics.
    """

    def __init__(self, status, message):
        text = f"HTTP {status}: {message}" if status is not None else message
        super().__init__(text)
        self.status = status
        self.message = message


class GhApi:
    """`gh api` runner.

    `attempts` bounds total tries per request (1 disables retries) and
    `backoff` holds the sleep seconds between tries; both exist so tests
    can run retry paths without wall-clock waits.
    """

    def __init__(self, attempts=3, backoff=(0.5, 1.0)):
        self.attempts = max(1, attempts)
        self.backoff = tuple(backoff)

    def request(self, method, path, body=None):
        """Run one `gh api` call; returns the parsed JSON or `None`.

        Raises `TransportError` when the call fails with a client
        error, exhausts its retries, or fails fast on a method that is
        unsafe to replay.
        """
        method = method.upper()
        cmd = ["gh", "api", "--method", method, path]
        payload = None
        if body is not None:
            cmd += ["--input", "-", "-H", "Content-Type: application/json"]
            payload = json.dumps(body).encode("utf-8")

        failure = None
        for attempt in range(self.attempts):
            if attempt and attempt <= len(self.backoff):
                time.sleep(self.backoff[attempt - 1])
            try:
                proc = subprocess.run(
                    cmd, input=payload, capture_output=True, timeout=_TIMEOUT_SECONDS
                )
            except subprocess.TimeoutExpired:
                failure = TransportError(None, "gh api timed out")
                if method not in _IDEMPOTENT:
                    # The request was already sent; a replay can
                    # duplicate the write.
                    raise failure
                continue
            except OSError as exc:
                # A missing or unspawnable `gh` cannot recover by retrying.
                raise TransportError(None, f"could not run gh: {exc}") from exc
            if proc.returncode == 0:
                out = proc.stdout.decode("utf-8", "replace").strip()
                if not out:
                    return None
                try:
                    return json.loads(out)
                except ValueError as exc:
                    raise TransportError(None, f"unparseable gh api output: {exc}") from exc
            stderr = proc.stderr.decode("utf-8", "replace").strip()
            found = _HTTP_STATUS.search(stderr)
            failure = TransportError(int(found.group(1)) if found else None, stderr)
            if failure.status is not None and failure.status < 500:
                raise failure
            if method not in _IDEMPOTENT:
                # A 5xx or status-less failure does not prove the write
                # was rejected; a replay can duplicate it.
                raise failure
        raise failure

"""Exercise baseline recovery with local Git history and no network access."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "ensure_diff_base.sh"


class DiffBaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1",
                        GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
        self.git(self.source, "init", "-b", "main")
        self.git(self.source, "config", "user.name", "Test")
        self.git(self.source, "config", "user.email", "test@example.test")
        self.commit("common.rs")
        self.git(self.source, "branch", "feature")
        self.commit("base.rs")
        self.base = self.git(self.source, "rev-parse", "HEAD").stdout.strip()
        self.git(self.source, "checkout", "feature")
        self.commit("feature.rs")

        self.checkout = self.root / "checkout"
        self.git(self.root, "clone", "--depth=1", "--branch=feature",
                 self.source.as_uri(), str(self.checkout))

    def git(self, directory, *args):
        return subprocess.run(["git", "-C", str(directory), *args],
                              env=self.env, capture_output=True, text=True, check=True)

    def commit(self, name):
        (self.source / name).write_text("fn example() {}\n")
        self.git(self.source, "add", name)
        self.git(self.source, "commit", "-m", name)

    def recover(self):
        return subprocess.run(["bash", str(SCRIPT), self.base], cwd=self.checkout,
                              env=self.env, capture_output=True, text=True)

    def test_diff_should_match_full_history_when_checkout_is_shallow(self):
        head = self.git(self.checkout, "rev-parse", "HEAD").stdout

        result = self.recover()

        self.assertEqual(result.returncode, 0, result.stderr)
        expected = self.git(self.source, "diff", self.base + "...HEAD").stdout
        actual = self.git(self.checkout, "diff", self.base + "...HEAD").stdout
        self.assertEqual(actual, expected)
        self.assertIn("feature.rs", actual)
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD").stdout, head)

    def test_recovery_should_succeed_when_base_exists_but_ancestry_is_missing(self):
        self.git(self.checkout, "fetch", "--depth=1", "origin", self.base)

        result = self.recover()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.git(self.checkout, "merge-base", self.base, "HEAD")

    def test_recovery_should_skip_fetch_when_merge_base_is_available(self):
        self.git(self.checkout, "fetch", "--unshallow", "origin", self.base)
        self.git(self.checkout, "remote", "remove", "origin")

        result = self.recover()

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_recovery_should_fail_when_origin_is_unavailable(self):
        self.git(self.checkout, "remote", "remove", "origin")

        result = self.recover()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not fetch diff baseline", result.stderr)

    def test_recovery_should_fail_when_histories_are_unrelated(self):
        self.git(self.source, "checkout", "--orphan", "unrelated")
        self.commit("unrelated.rs")
        self.base = self.git(self.source, "rev-parse", "HEAD").stdout.strip()

        result = self.recover()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("has no merge-base with HEAD", result.stderr)

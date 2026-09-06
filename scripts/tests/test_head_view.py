"""Committed-head collection: worktree isolation, dry run, fail-closed."""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import head_view  # noqa: E402

HEAD_LINE = "fn head_commit() {}"
DIRTY_LINE = "fn dirty_tree() {}"


def write_stub(path, script):
    path.write_text(script)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class HeadViewTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.real_git = shutil.which("git")
        self.git("init", "-q")
        self.git("config", "user.email", "ci@test")
        self.git("config", "user.name", "ci")
        (self.repo / "lib.rs").write_text(HEAD_LINE + "\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "head")
        self.head_sha = self.git("rev-parse", "HEAD").stdout.strip()

        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        # The tidy stub: in --dry-run mode it reports the file's actual
        # first line, so the test can tell committed content (the head
        # worktree) from mutated content (the dirty main checkout).
        self.tidy = self.bin_dir / "tidy"
        write_stub(self.tidy, """#!/usr/bin/env bash
for a in "$@"; do [ "$a" = "--dry-run" ] && dry=1; done
if [ "${dry:-0}" = 1 ]; then
  line="$(head -n1 lib.rs)"
  printf '[{"path":"lib.rs","line":1,"severity":"error","code":"DOC001","message":"committed: %s","item_kind":"fn","item_name":"hello","title":"missing documentation"}]\\n' "$line"
  exit 1
fi
exit 1
""")
        # A git wrapper that flags any `submodule update` invocation: the
        # head view must never fetch PR-controlled submodule URLs.
        self.marker = self.root / "submodule-calls"
        write_stub(self.bin_dir / "git", f"""#!/usr/bin/env bash
case "$*" in
  *"submodule update"*) printf 'submodule-update\\n' >> "{self.marker}";;
esac
exec "{self.real_git}" "$@"
""")
        self.args_file = self.root / "args"
        self.args_file.write_bytes(b"--output-mode\0json\0.\0")
        self.out = self.root / "head-run.json"

        self._path = os.environ.get("PATH")
        os.environ["PATH"] = f"{self.bin_dir}:{self._path}"

    def tearDown(self):
        os.environ["PATH"] = self._path
        self._tmp.cleanup()

    def git(self, *argv):
        """One real-git call in the fixture repository."""
        return self.git_at(self.repo, *argv)

    def git_at(self, repo_dir, *argv):
        """One real-git call in an arbitrary fixture repository."""
        return subprocess.run(
            [self.real_git, "-C", str(repo_dir), *argv],
            capture_output=True, text=True, check=True,
        )

    def collect(self, binary=None, revision=None, repo_dir=None):
        return head_view.main([
            "--repository-dir", str(repo_dir or self.repo),
            "--revision", revision or self.head_sha,
            "--args-file", str(self.args_file),
            "--binary", str(binary or self.tidy),
            "--output", str(self.out),
        ])

    def test_collects_committed_findings_not_the_dirty_tree(self):
        (self.repo / "lib.rs").write_text(DIRTY_LINE + "\n")
        self.assertEqual(self.collect(), 0)

        records = json.loads(self.out.read_text())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["message"], f"committed: {HEAD_LINE}")

        # The dirty main checkout is untouched, the temporary worktree is
        # fully cleaned up, and no submodule fetch was attempted.
        self.assertEqual((self.repo / "lib.rs").read_text(), DIRTY_LINE + "\n")
        listing = self.git("worktree", "list").stdout
        self.assertEqual(len(listing.strip().splitlines()), 1)
        self.assertFalse(self.marker.exists())

    def test_collects_from_the_project_subdirectory_of_the_worktree(self):
        # rust-project-path may sit below the repository root; saved paths
        # and record paths are relative to it, so the head-view run must
        # execute in the worktree's matching subdirectory, not its root.
        project = self.repo / "proj"
        project.mkdir()
        (project / "lib.rs").write_text(HEAD_LINE + "\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "add project subdir")
        head = self.git("rev-parse", "HEAD").stdout.strip()

        tidy_cwd = self.bin_dir / "tidy-cwd"
        write_stub(tidy_cwd, """#!/usr/bin/env bash
for a in "$@"; do [ "$a" = "--dry-run" ] && dry=1; done
if [ "${dry:-0}" = 1 ]; then
  printf '[{"path":"lib.rs","line":1,"severity":"error","code":"DOC001","message":"cwd:%s","item_kind":"fn","item_name":"hello","title":"missing documentation"}]\\n' "$(basename "$PWD")"
  exit 1
fi
exit 1
""")

        self.assertEqual(self.collect(binary=tidy_cwd, repo_dir=project,
                                      revision=head), 0)

        records = json.loads(self.out.read_text())
        self.assertEqual(records[0]["message"], "cwd:proj")

    def test_repository_with_submodules_fails_closed(self):
        # .gitmodules at the collected revision means the worktree cannot
        # cover submodule content; a partial document would later read as
        # cleared findings, so collection refuses.
        sub = self.root / "sub-origin"
        sub.mkdir()
        for argv in (("init", "-q"), ("config", "user.email", "ci@test"),
                     ("config", "user.name", "ci")):
            self.git_at(sub, *argv)
        (sub / "lib.rs").write_text("fn sub() {}\n")
        self.git_at(sub, "add", "-A")
        self.git_at(sub, "commit", "-qm", "sub")
        # Local-path submodule adds need the file transport, which modern
        # git disables by default; allow it for this offline fixture.
        self.git_at(self.repo, "-c", "protocol.file.allow=always",
                    "submodule", "add", "-q", str(sub), "subm")
        self.git("commit", "-qm", "add submodule")
        head = self.git("rev-parse", "HEAD").stdout.strip()

        self.assertEqual(self.collect(revision=head), 1)
        self.assertFalse(self.out.exists())
        # No submodule fetch was attempted here either.
        self.assertFalse(self.marker.exists())

    def test_bad_revision_and_unparseable_output_fail_closed(self):
        # A resolvable non-hex ref is refused before any worktree exists;
        # "HEAD" would collect fine without the gate, pinning it.
        self.assertEqual(self.collect(revision="HEAD"), 1)
        self.assertFalse(self.out.exists())

        broken = self.bin_dir / "not-json"
        write_stub(broken, '#!/usr/bin/env bash\nprintf "not json\\n"\n')
        self.assertEqual(self.collect(binary=broken), 1)
        self.assertFalse(self.out.exists())


if __name__ == "__main__":
    unittest.main()

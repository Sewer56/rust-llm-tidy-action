"""Run-step wiring: argument building and publication of each outcome.

Runs the action's real entrypoint (scripts/run_tidy.py) offline.
Fixtures match the CI harness: a local Git repository, bare remote,
recording `gh` stub, and stub tidy binaries.

Asserts the run end to end:

- the built argv mirrors the inputs: read-only selection, op lists,
  config flags, and processing targets (explicit paths, the PR's changed
  files, or the whole project);
- the run rejects a binary whose --help lacks --checks-only before it runs;
- a successful apply push publishes the run document at the pushed
  revision, with no change-report flood on first publication;
- a failed push publishes the head view; check publishes its read-only scan;
- a merge checkout (HEAD is not the PR head) publishes a head-view scan
  describing the PR head's bytes, not the merge commit's;
- a clean-tree PR run publishes hints and reminders at HEAD;
- non-PR and validate runs publish nothing.
"""

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

import sticky_publish  # noqa: E402

ACTION_DIR = Path(__file__).resolve().parents[2]
RUN_ENTRY = ACTION_DIR / "scripts" / "run_tidy.py"
SERVER = "https://github.test"

# `gh api` stub: answers the sticky-publish REST surfaces offline and
# records every invocation.
#
# The PR head follows the remote branch, then RLT_HEAD_SHA, then HEAD.
# This lets revision checks observe commits pushed by the run or fixture.
GH_STUB = """#!/usr/bin/env bash
if [ "$1" = "api" ]; then
  printf '%s\\n' "$*" >> "__API_LOG__"
  head="$(git rev-parse --verify origin/feature 2>/dev/null \\
          || printf '%s' "${RLT_HEAD_SHA:-$(git rev-parse HEAD)}")"
  case "$*" in
    *"--method GET user"*) printf '{"login":"ci-bot"}\\n' ;;
    *"pulls/7"*) printf '{"number":7,"head":{"sha":"%s"}}\\n' "$head" ;;
    *"--method POST"*|*"--method PATCH"*)
      cat > "__API_BODY__"
      printf '{"id":42,"html_url":"__SERVER__/o/r/issues/7#issuecomment-42"}\\n' ;;
    *) printf '[]\\n' ;;
  esac
  exit 0
fi
f="$(echo "$@" | sed -n 's/.*--body-file \\([^ ]*\\).*/\\1/p')"
printf '%s\\n' "$f" >> "__PR_LOG__"
[ -n "$f" ] && cat "$f" >> "__PR_LOG__"
exit 0
"""

# Probe answer for the run step's gate: the action requires a binary
# whose --help advertises --checks-only, so every stub must match that.
CAPABLE_HELP = """if [ "$1" = "--help" ]; then
  printf '%s\\n' "      --checks-only    Run only lint checks"
  exit 0
fi
"""

# Recording stub: logs the argv it received, reports no findings.
TIDY_RECORD_STUB = "#!/usr/bin/env bash\n" + CAPABLE_HELP + """printf '%s\\n' "$*" >> "__CALLS__"
printf '[]\\n'
exit 0
"""

# Tidy stub: apply rewrites lib.rs; read-only scans (--dry-run main runs,
# --checks-only head views) report its first line unchanged.
#
# Like the real CLI, the flag scan stops at `--`, so flag-shaped target
# names stay paths.
#
# Distinct messages identify whether a report used local or committed code.
TIDY_STUB = "#!/usr/bin/env bash\n" + CAPABLE_HELP + """for a in "$@"; do
  case "$a" in
    --) break ;;
    --dry-run|--checks-only) dry=1 ;;
  esac
done
if [ "${dry:-0}" = 1 ]; then
  line="$(head -n1 lib.rs)"
  printf '[{"path":"lib.rs","line":1,"severity":"error","code":"DOC001","message":"committed: %s","item_kind":"fn","item_name":"hello","title":"missing documentation"}]\\n' "$line"
  exit 1
fi
printf 'fn main() { init(); }\\nfn init() {}\\n' > lib.rs
printf '[{"path":"lib.rs","line":1,"severity":"error","code":"DOC001","message":"mutated tree finding","item_kind":"fn","item_name":"hello","title":"missing documentation"}]\\n'
exit 1
"""

# Clean-tree stub: one advisory hint, no mutation, exit 0.
TIDY_HINT_STUB = "#!/usr/bin/env bash\n" + CAPABLE_HELP + """printf '[{"path":"lib.rs","line":1,"severity":"hint","code":"DOC999","message":"consider pre-allocating the buffer","item_kind":"fn","item_name":"hello","title":"advisory follow-up"}]\\n'
exit 0
"""

GIT_STUB = """#!/usr/bin/env bash
if [ "$1" = "push" ] && [ "${RLT_PUSH_FAIL:-0}" = "1" ]; then
  echo "error: simulated push failure" >&2
  exit 1
fi
exec "__REAL_GIT__" "$@"
"""


def write_stub(path, script):
    path.write_text(script)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class TidyArgvTests(unittest.TestCase):
    """The tidy argv the entrypoint builds from the action's inputs."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.temp = self.root / "runner-temp"
        self.bin_dir = self.root / "bin"
        self.repo = self.root / "repo"
        self.temp.mkdir()
        self.bin_dir.mkdir()
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "ci@test")
        self.git("config", "user.name", "ci")
        (self.repo / "lib.rs").write_text("fn main() { init(); }\nfn init() {}\n")
        (self.repo / "other.rs").write_text("fn other() {}\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "tidy-base")
        self.base = self.git("rev-parse", "HEAD").stdout.strip()
        (self.repo / "lib.rs").write_text("fn a() {}\nfn b() { a(); }\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "untidy")
        self.head = self.git("rev-parse", "HEAD").stdout.strip()

        self.calls = self.temp / "tidy-calls"
        self.tidy = self.bin_dir / "tidy-record"
        write_stub(self.tidy,
                   TIDY_RECORD_STUB.replace("__CALLS__", str(self.calls)))

    def tearDown(self):
        self._tmp.cleanup()

    def git(self, *argv):
        """One real-git call in the fixture repository."""
        return subprocess.run(
            ["git", "-C", str(self.repo), *argv],
            capture_output=True, text=True, check=True,
        )

    def run_entry(self, **overrides):
        """Run the entrypoint; extra kwargs override the env.

        A `None` value removes the variable, standing in for an unset
        one. Returns the process result and the argv lines the tidy
        binary recorded.
        """
        env = dict(
            os.environ, RUNNER_TEMP=str(self.temp), RLT_BIN=str(self.tidy),
            RLT_MODE="apply", RLT_VALIDATE="false", RLT_NO_CONFIG="false",
            RLT_DRY_RUN="false", RLT_CONFIG_PATH="", RLT_INCLUDE="",
            RLT_EXCLUDE="", RLT_PATH_LIST="", RLT_DEFAULT_FILES="",
            RLT_CHANGED_FILES="false", IS_PR="false",
        )
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        if self.calls.exists():
            self.calls.unlink()
        result = subprocess.run(
            [sys.executable, str(RUN_ENTRY)], cwd=self.repo, env=env,
            capture_output=True, text=True,
        )
        calls = (self.calls.read_text().splitlines()
                 if self.calls.exists() else [])
        return result, calls

    def test_arguments_should_select_read_only_mode_without_duplicate_flags(self):
        cases = (
            ("apply", "apply", "false", "false", "--output-mode json -- ."),
            ("check", "check", "false", "false",
             "--output-mode json --dry-run -- ."),
            ("explicit", "apply", "true", "false",
             "--output-mode json --dry-run -- ."),
            ("both", "check", "true", "false",
             "--output-mode json --dry-run -- ."),
            ("validate", "check", "true", "true", "--validate"),
        )

        for name, mode, dry_run, validate, expected in cases:
            with self.subTest(name=name):
                result, calls = self.run_entry(
                    RLT_MODE=mode, RLT_DRY_RUN=dry_run, RLT_VALIDATE=validate)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(calls, [expected])

    def test_op_lists_should_split_trim_and_skip_empty_entries(self):
        # Inner spaces are part of one value; only separators and outer
        # whitespace split entries.
        cases = (
            ("include_commas_and_semis", "RLT_INCLUDE", "reorder, vis ; lints",
             "--include reorder --include vis --include lints"),
            ("include_blank_lines", "RLT_INCLUDE", "reorder\n\n  vis  \n",
             "--include reorder --include vis"),
            ("include_inner_spaces_stay", "RLT_INCLUDE", "reorder  vis",
             "--include reorder  vis"),
            ("exclude_split", "RLT_EXCLUDE", "lints;DOC008",
             "--exclude lints --exclude DOC008"),
        )

        for name, key, raw, expected in cases:
            with self.subTest(list=name):
                result, calls = self.run_entry(**{key: raw})

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    calls, [f"--output-mode json {expected} -- ."])

    def test_explicit_paths_should_win_and_skip_blank_lines(self):
        result, calls = self.run_entry(RLT_PATH_LIST="src\n  lib.rs \n\n")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ["--output-mode json -- src lib.rs"])

    def test_config_flags_should_mirror_the_inputs(self):
        cases = (
            ("none", "", "false", "--output-mode json -- ."),
            ("explicit", "conf/tidy.yml", "false",
             "--config conf/tidy.yml --output-mode json -- ."),
            ("no_config_wins", "conf/tidy.yml", "true",
             "--no-config --output-mode json -- ."),
        )

        for name, config_path, no_config, expected in cases:
            with self.subTest(name=name):
                result, calls = self.run_entry(
                    RLT_CONFIG_PATH=config_path, RLT_NO_CONFIG=no_config)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(calls, [expected])

    def test_changed_files_should_select_the_pr_diff_against_the_base(self):
        result, calls = self.run_entry(RLT_CHANGED_FILES="true",
                                       RLT_DEFAULT_FILES=self.base)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ["--output-mode json -- lib.rs"])

    def test_changed_files_without_a_pr_should_pass_no_paths(self):
        # Non-PR runs let the CLI use the working-tree diff.
        result, calls = self.run_entry(RLT_CHANGED_FILES="true")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ["--output-mode json"])

    def test_changed_files_should_fail_when_the_baseline_is_unreachable(self):
        result, calls = self.run_entry(RLT_CHANGED_FILES="true",
                                       RLT_DEFAULT_FILES="f" * 40)

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("could not fetch diff baseline", result.stderr)
        self.assertEqual(calls, [])

    def test_run_should_reject_invalid_environment_inputs(self):
        cases = (
            ("mode", {"RLT_MODE": "tidy"}),
            ("boolean", {"RLT_DRY_RUN": "maybe"}),
            ("temp", {"RUNNER_TEMP": ""}),
            ("binary", {"RLT_BIN": ""}),
        )

        for name, overrides in cases:
            with self.subTest(input=name):
                result, calls = self.run_entry(**overrides)

                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("::error::rust-llm-tidy:", result.stderr)
                self.assertEqual(calls, [])


class RunStepTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.temp = self.root / "runner-temp"
        self.bin_dir = self.root / "bin"
        self.temp.mkdir()
        self.bin_dir.mkdir()
        self.real_git = shutil.which("git")

        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "--bare", str(self.root / "origin"))
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "ci@test")
        self.git("config", "user.name", "ci")
        self.git("remote", "add", "origin", str(self.root / "origin"))
        (self.repo / "lib.rs").write_text("fn main() { init(); }\nfn init() {}\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "tidy-base")

        self.api_log = self.temp / "gh-api.log"
        self.api_body = self.temp / "gh-api-body.json"
        self.pr_log = self.temp / "gh-pr-comment.log"
        write_stub(self.bin_dir / "gh", GH_STUB
                   .replace("__API_LOG__", str(self.api_log))
                   .replace("__API_BODY__", str(self.api_body))
                   .replace("__PR_LOG__", str(self.pr_log))
                   .replace("__SERVER__", SERVER))
        self.tidy = self.bin_dir / "tidy"
        write_stub(self.tidy, TIDY_STUB)
        self.tidy_hint = self.bin_dir / "tidy-hint"
        write_stub(self.tidy_hint, TIDY_HINT_STUB)
        write_stub(self.bin_dir / "git",
                   GIT_STUB.replace("__REAL_GIT__", self.real_git))

    def tearDown(self):
        self._tmp.cleanup()

    def git(self, *argv):
        """One real-git call in the fixture repository."""
        return subprocess.run(
            [self.real_git, "-C", str(self.repo), *argv],
            capture_output=True, text=True, check=True,
        )

    def run_action(self, **env):
        """Run the run-step entrypoint; extra kwargs override the env.

        A `None` value removes the variable, standing in for an unset one.
        """
        script_env = dict(
            os.environ,
            PATH=f"{self.bin_dir}:{os.environ['PATH']}",
            RUNNER_TEMP=str(self.temp),
            RLT_MODE="apply", IS_PR="true", PR_NUMBER="7", HEAD_REF="feature",
            COMMIT_NAME="bot", COMMIT_EMAIL="b@b", GITHUB_TOKEN="x",
            RLT_BIN=str(self.tidy), RLT_VALIDATE="false",
            RLT_NO_CONFIG="false", RLT_DRY_RUN="false", RLT_CONFIG_PATH="",
            RLT_INCLUDE="", RLT_EXCLUDE="", RLT_PATH_LIST="",
            RLT_DEFAULT_FILES="", RLT_CHANGED_FILES="false",
            GITHUB_REPOSITORY="o/r", GITHUB_SERVER_URL=SERVER,
        )
        script_env.pop("RLT_HEAD_SHA", None)
        script_env.pop("RLT_PUSH_FAIL", None)
        script_env.pop("RUST_LLM_TIDY_DIFF_BASE", None)
        script_env.pop("RLT_PR_BASE", None)
        for key, value in env.items():
            if value is None:
                script_env.pop(key, None)
            else:
                script_env[key] = value
        return subprocess.run(
            [sys.executable, str(RUN_ENTRY)], cwd=self.repo, env=script_env,
            capture_output=True, text=True,
        )

    def untidy_commit_on_feature(self):
        """An untidy commit pushed to origin/feature; returns its SHA."""
        (self.repo / "lib.rs").write_text("fn b() { a(); }\nfn a() {}\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "untidy")
        self.git("push", "-q", "origin", "HEAD:refs/heads/feature")
        return self.git("rev-parse", "HEAD").stdout.strip()

    def api_posts(self):
        if not self.api_log.exists():
            return []
        return [line for line in self.api_log.read_text().splitlines()
                if "--method POST" in line]

    def posted_body(self):
        """The Markdown body of the last posted comment."""
        return json.loads(self.api_body.read_text())["body"]

    def test_run_should_reject_invalid_environment_before_running(self):
        cases = (
            ("mode", {"RLT_MODE": "tidy"}),
            ("pr_flag", {"IS_PR": "maybe"}),
            ("binary", {"RLT_BIN": ""}),
            ("pr_number", {"PR_NUMBER": "abc"}),
            ("head_ref", {"HEAD_REF": None}),
        )

        for name, overrides in cases:
            with self.subTest(input=name):
                result = self.run_action(**overrides)

                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("::error::rust-llm-tidy:", result.stderr)
                self.assertFalse((self.temp / "rlt-run.json").exists())
                self.assertFalse(self.api_log.exists())

    def test_run_should_default_unset_flags_to_false_for_standalone_runs(self):
        # The script harness runs without the action's exported flags;
        # unset booleans mean non-PR and non-validate like the old script.
        result = self.run_action(
            IS_PR=None, RLT_VALIDATE=None, RLT_MODE=None, RLT_NO_CONFIG=None,
            RLT_DRY_RUN=None, RLT_CHANGED_FILES=None,
            RLT_BIN=str(self.tidy_hint),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.api_log.exists())

    def test_run_should_fail_concisely_when_binary_lacks_checks_only(self):
        # An older binary answers --help without --checks-only; the gate
        # must reject it before it can run or mutate anything.
        write_stub(self.tidy, """#!/usr/bin/env bash
if [ "$1" = "--help" ]; then
  printf '%s\\n' "Usage: rust-llm-tidy [OPTIONS] [PATH]..." "      --dry-run    Preview without modifying files"
  exit 0
fi
exit 0
""")

        result = self.run_action()

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("--checks-only", result.stderr)
        self.assertIn("binary-source: git", result.stderr)
        self.assertFalse((self.temp / "rlt-run.json").exists())
        self.assertEqual(self.git("status", "--porcelain").stdout, "")
        self.assertFalse(self.api_log.exists())
        self.assertFalse(self.pr_log.exists())

    def test_run_should_report_the_real_error_when_binary_cannot_run(self):
        # A missing binary fails the probe; the error keeps the spawn
        # cause and exit status instead of blaming --checks-only support.
        result = self.run_action(RLT_BIN=str(self.bin_dir / "missing-tidy"))

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("--help failed (exit 127)", result.stderr)
        self.assertIn("No such file or directory", result.stderr)
        self.assertNotIn("binary-source: git", result.stderr)
        self.assertFalse((self.temp / "rlt-run.json").exists())

    def test_read_only_should_fail_on_proposed_edits_without_committing(self):
        head_sha = self.untidy_commit_on_feature()
        # The required CLI fails its own dry-run when transformations are
        # needed; the action passes that status through without touching
        # the tree.
        write_stub(self.tidy, """#!/usr/bin/env bash
if [ "$1" = "--help" ]; then
  printf '%s\\n' "      --checks-only    Run only lint checks"
  exit 0
fi
printf '%s\\n' '[{"path":"lib.rs","severity":"success","code":"REORDER","message":"move item"}]'
exit 1
""")
        cases = (("check", "check", "false"), ("explicit", "apply", "true"))

        for name, mode, dry_run in cases:
            with self.subTest(name=name):
                result = self.run_action(
                    RLT_MODE=mode, RLT_DRY_RUN=dry_run, RLT_HEAD_SHA=head_sha)

                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("### Changes", self.pr_log.read_text())
                self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), head_sha)
                self.assertEqual(self.git("status", "--porcelain").stdout, "")
                self.assertFalse((self.temp / "rlt-head-run.json").exists())

    def test_read_only_should_pass_when_no_edits_or_errors_remain(self):
        cases = (("check", "check", "false"), ("explicit", "apply", "true"))

        for name, mode, dry_run in cases:
            with self.subTest(name=name):
                result = self.run_action(
                    RLT_MODE=mode, RLT_DRY_RUN=dry_run,
                    RLT_BIN=str(self.tidy_hint))

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("### Hints", self.posted_body())
                self.assertEqual(self.git("status", "--porcelain").stdout, "")

    def test_pr_should_reject_preexisting_edits_before_running_or_publishing(self):
        head_sha = self.untidy_commit_on_feature()
        (self.repo / "lib.rs").write_text("// staged user work\n")
        self.git("add", "lib.rs")
        (self.repo / "lib.rs").write_text("// unstaged user work\n")
        (self.repo / "new.rs").write_text("// untracked user work\n")
        before = self.git("diff", "HEAD").stdout
        index = self.git("diff", "--cached").stdout
        cases = (("apply", "apply", "false"), ("check", "check", "false"),
                 ("explicit", "apply", "true"))

        for name, mode, dry_run in cases:
            with self.subTest(name=name):
                result = self.run_action(RLT_MODE=mode, RLT_DRY_RUN=dry_run)

                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("require a clean checkout", result.stderr)
                self.assertFalse((self.temp / "rlt-run.json").exists())
                self.assertFalse(self.api_log.exists())
                self.assertFalse(self.pr_log.exists())
                self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), head_sha)
                self.assertEqual(self.git("diff", "HEAD").stdout, before)
                self.assertEqual(self.git("diff", "--cached").stdout, index)
                self.assertEqual((self.repo / "new.rs").read_text(), "// untracked user work\n")

    def test_validate_pr_run_should_exempt_preexisting_edits(self):
        self.untidy_commit_on_feature()
        (self.repo / "lib.rs").write_text("// unstaged user work\n")
        # Swap only the run's trailing exit 0; the --help prelude keeps
        # exiting 0 so the probe still passes.
        write_stub(self.tidy_hint,
                   TIDY_HINT_STUB.rsplit("exit 0", 1)[0] + "exit 7\n")

        result = self.run_action(RLT_VALIDATE="true",
                                 RLT_BIN=str(self.tidy_hint))

        # The clean-checkout gate exempts validate runs: the tool runs,
        # its own exit status passes through, nothing is published.
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertTrue((self.temp / "rlt-run.json").exists())
        self.assertEqual((self.repo / "lib.rs").read_text(),
                         "// unstaged user work\n")
        self.assertFalse(self.api_log.exists())
        self.assertFalse(self.pr_log.exists())

    def test_non_pr_dry_run_should_preserve_preexisting_edits(self):
        (self.repo / "lib.rs").write_text("// user work\n")
        self.git("add", "lib.rs")
        before = self.git("diff", "--cached").stdout

        result = self.run_action(RLT_DRY_RUN="true", IS_PR="false",
                                 RLT_BIN=str(self.tidy_hint))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("diff", "--cached").stdout, before)
        self.assertEqual((self.repo / "lib.rs").read_text(), "// user work\n")
        self.assertFalse(self.api_log.exists())

    def test_apply_should_preserve_lint_failure_after_successful_push(self):
        head_sha = self.untidy_commit_on_feature()
        # The real action always exports the pre-push PR head; the report
        # must still describe the pushed revision, never that stale head.
        result = self.run_action(RLT_HEAD_SHA=head_sha)
        self.assertEqual(result.returncode, 1, result.stderr)

        posts = self.api_posts()
        self.assertEqual(len(posts), 1, posts)  # sticky only, no delta flood
        pushed = self.git("rev-parse", "HEAD").stdout.strip()
        body = self.posted_body()
        self.assertIn(f"blob/{pushed}/lib.rs#L1", body)
        self.assertNotIn(f"blob/{head_sha}/", body)
        state = sticky_publish.decode_state(body)
        self.assertEqual(
            state["findings"][0]["record"]["message"], "mutated tree finding")
        self.assertFalse(self.pr_log.exists())  # success: no failure comment

    def test_apply_should_pass_when_fixes_push_without_lint_failure(self):
        head_sha = self.untidy_commit_on_feature()
        write_stub(self.tidy, TIDY_STUB.replace("exit 1", "exit 0"))

        result = self.run_action(RLT_HEAD_SHA=head_sha)

        self.assertEqual(result.returncode, 0, result.stderr)
        pushed = self.git("rev-parse", "origin/feature").stdout.strip()
        self.assertNotEqual(pushed, head_sha)
        self.assertIn(f"blob/{pushed}/lib.rs#L1", self.posted_body())

    def test_flag_shaped_changed_files_should_still_push_and_publish(self):
        # A PR may change flag-shaped file names. The `--` separator
        # keeps them positional; routing uses validated inputs, so apply
        # still pushes and publishes the fixes.
        base = self.git("rev-parse", "HEAD").stdout.strip()
        self.untidy_commit_on_feature()
        for name in ("--", "--dry-run"):
            (self.repo / name).write_text("flag-shaped\n")
        self.git("add", "--", "--", "--dry-run")
        self.git("commit", "-qm", "flag-shaped names")
        self.git("push", "-q", "origin", "HEAD:refs/heads/feature")
        head_sha = self.git("rev-parse", "HEAD").stdout.strip()

        result = self.run_action(RLT_CHANGED_FILES="true",
                                 RLT_DEFAULT_FILES=base,
                                 RLT_HEAD_SHA=head_sha)

        self.assertEqual(result.returncode, 1, result.stderr)
        pushed = self.git("rev-parse", "HEAD").stdout.strip()
        self.assertNotEqual(pushed, head_sha)  # apply pushed the fix commit
        self.assertEqual(len(self.api_posts()), 1)
        body = self.posted_body()
        self.assertIn(f"blob/{pushed}/lib.rs#L1", body)
        state = sticky_publish.decode_state(body)
        self.assertEqual(state["findings"][0]["record"]["message"],
                         "mutated tree finding")

    def test_failed_push_publishes_head_findings_at_the_pr_head(self):
        head_sha = self.untidy_commit_on_feature()
        result = self.run_action(RLT_PUSH_FAIL="1", RLT_HEAD_SHA=head_sha)
        self.assertEqual(result.returncode, 1, result.stdout)

        # The operational failure information is preserved.
        self.assertIn("could not push fixes", self.pr_log.read_text())
        self.assertEqual(len(self.api_posts()), 1)
        body = self.posted_body()
        self.assertIn(f"blob/{head_sha}/lib.rs#L1", body)
        # The report describes the committed PR head, not the local fixes.
        state = sticky_publish.decode_state(body)
        self.assertEqual(state["findings"][0]["record"]["message"],
                         "committed: fn b() { a(); }")

    def test_check_failure_publishes_head_findings_at_the_pr_head(self):
        head_sha = self.untidy_commit_on_feature()
        result = self.run_action(RLT_MODE="check", RLT_HEAD_SHA=head_sha)
        self.assertEqual(result.returncode, 1, result.stdout)

        self.assertIn("not tidy", self.pr_log.read_text())
        self.assertEqual(len(self.api_posts()), 1)
        body = self.posted_body()
        self.assertIn(f"blob/{head_sha}/lib.rs#L1", body)
        state = sticky_publish.decode_state(body)
        self.assertEqual(state["findings"][0]["record"]["message"],
                         "committed: fn b() { a(); }")
        self.assertEqual(self.git("status", "--porcelain").stdout, "")
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), head_sha)
        self.assertFalse((self.temp / "rlt-head-run.json").exists())

    def test_check_failure_on_merge_checkout_publishes_pr_head_findings(self):
        head_sha = self.untidy_commit_on_feature()
        # Default PR checkouts scan a merge of the PR head into the base
        # branch. Keep the merge bytes different from the head's so the
        # consumed report distinguishes the two revisions.
        self.git("checkout", "-q", "HEAD~1")
        (self.repo / "lib.rs").write_text("// merge bytes\n")
        self.git("commit", "-qam", "base-side edit")
        self.git("merge", "-s", "ours", "-m", "merge PR", head_sha)
        merge_sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.assertNotEqual(merge_sha, head_sha)

        result = self.run_action(RLT_MODE="check", RLT_HEAD_SHA=head_sha)

        self.assertEqual(result.returncode, 1, result.stdout)
        # The failure comment still shows the run's merge scan; the
        # published report describes the PR head and pins to it.
        self.assertIn("not tidy", self.pr_log.read_text())
        self.assertIn("committed: // merge bytes", self.pr_log.read_text())
        self.assertEqual(len(self.api_posts()), 1)
        body = self.posted_body()
        self.assertIn(f"blob/{head_sha}/lib.rs#L1", body)
        self.assertNotIn(f"blob/{merge_sha}/", body)
        state = sticky_publish.decode_state(body)
        self.assertEqual(state["findings"][0]["record"]["message"],
                         "committed: fn b() { a(); }")
        self.assertEqual(self.git("status", "--porcelain").stdout, "")

    def test_check_success_on_merge_checkout_publishes_pr_head_findings(self):
        head_sha = self.untidy_commit_on_feature()
        self.git("checkout", "-q", "HEAD~1")
        (self.repo / "lib.rs").write_text("// merge bytes\n")
        self.git("commit", "-qam", "base-side edit")
        self.git("merge", "-s", "ours", "-m", "merge PR", head_sha)
        merge_sha = self.git("rev-parse", "HEAD").stdout.strip()
        self.assertNotEqual(merge_sha, head_sha)
        # A clean scan through the same merge-checkout branch: the report
        # still describes the PR head's bytes, not the merge commit's.
        write_stub(self.tidy, TIDY_STUB.replace("exit 1", "exit 0", 1))

        result = self.run_action(RLT_MODE="check", RLT_HEAD_SHA=head_sha)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.pr_log.exists())  # green: no failure comment
        self.assertEqual(len(self.api_posts()), 1)
        body = self.posted_body()
        self.assertIn(f"blob/{head_sha}/lib.rs#L1", body)
        self.assertNotIn(f"blob/{merge_sha}/", body)
        self.assertNotIn("merge bytes", body)
        state = sticky_publish.decode_state(body)
        self.assertEqual(state["findings"][0]["record"]["message"],
                         "committed: fn b() { a(); }")
        self.assertEqual(self.git("status", "--porcelain").stdout, "")

    def test_clean_pr_run_publishes_hints_at_head(self):
        result = self.run_action(RLT_BIN=str(self.tidy_hint))
        self.assertEqual(result.returncode, 0, result.stderr)

        self.assertEqual(len(self.api_posts()), 1)
        body = self.posted_body()
        self.assertIn("### Hints - consider looking at these", body)
        self.assertIn("1 hint.", body)
        state = sticky_publish.decode_state(body)
        self.assertEqual(state["findings"][0]["record"]["severity"], "hint")

    def test_clean_pr_run_should_publish_reminders_without_failing(self):
        write_stub(self.tidy_hint, TIDY_HINT_STUB.replace('"hint"', '"reminder"'))

        result = self.run_action(RLT_BIN=str(self.tidy_hint))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.api_posts()), 1)
        body = self.posted_body()
        self.assertIn("### Reminders\n", body)
        self.assertIn(sticky_publish.REMINDER_NOTE, body)
        self.assertIn("      consider pre-allocating the buffer", body)
        self.assertIn("1 reminder.", body)
        state = sticky_publish.decode_state(body)
        self.assertEqual(state["findings"][0]["record"]["severity"], "reminder")

    def test_non_pr_apply_run_publishes_nothing(self):
        self.untidy_commit_on_feature()
        result = self.run_action(IS_PR="false")
        self.assertEqual(result.returncode, 1)  # the stub exits 1
        self.assertFalse(self.api_log.exists())

    def test_validate_run_publishes_nothing(self):
        result = self.run_action(RLT_VALIDATE="true",
                                 RLT_BIN=str(self.tidy_hint))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.api_log.exists())

    def test_publication_failure_does_not_change_the_run_outcome(self):
        # An invalid repository drives sticky_publish to its usage
        # error; the wiring must warn and keep the tidy exit status.
        result = self.run_action(RLT_BIN=str(self.tidy_hint),
                                 GITHUB_REPOSITORY="not a repo")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("could not publish the findings report", result.stderr)

    def test_run_should_supply_baseline_when_pr_context_is_available(self):
        baseline_log = self.temp / "baseline"
        write_stub(self.tidy_hint, """#!/usr/bin/env bash
if [ "$1" = "--help" ]; then
  printf '%s\\n' "      --checks-only    Run only lint checks"
  exit 0
fi
printf '%s' "${RUST_LLM_TIDY_DIFF_BASE-unset}" > "$RUNNER_TEMP/baseline"
printf '[]\\n'
""")
        cases = (
            ("pr_default", "true", "pr-base", None, "pr-base"),
            ("pr_override", "true", "pr-base", "custom", "custom"),
            ("pr_empty_override", "true", "pr-base", "", "pr-base"),
            ("non_pr", "false", "pr-base", None, "unset"),
            ("non_pr_override", "false", "", "custom", "custom"),
            ("missing_base", "true", "", None, "unset"),
        )

        for name, is_pr, base, override, expected in cases:
            with self.subTest(name=name):
                env = dict(IS_PR=is_pr, RLT_PR_BASE=base,
                           RLT_BIN=str(self.tidy_hint), RLT_VALIDATE="true")
                if override is not None:
                    env["RUST_LLM_TIDY_DIFF_BASE"] = override

                result = self.run_action(**env)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(baseline_log.read_text(), expected)

    def test_check_should_scan_once_with_pr_baseline(self):
        head_sha = self.untidy_commit_on_feature()
        script = TIDY_STUB.replace(
            'for a in "$@";',
            'printf "%s\\n" "${RUST_LLM_TIDY_DIFF_BASE-unset}" '
            '>> "$RUNNER_TEMP/baselines"\nfor a in "$@";',
        )
        write_stub(self.tidy, script)

        result = self.run_action(RLT_MODE="check", RLT_HEAD_SHA=head_sha,
                                 RLT_PR_BASE=head_sha)

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual((self.temp / "baselines").read_text().splitlines(),
                         [head_sha])


if __name__ == "__main__":
    unittest.main()

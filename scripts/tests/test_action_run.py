"""Run-script wiring: which document and revision each outcome publishes.

Runs the "Run rust-llm-tidy" script extracted from action.yml offline.
Fixtures match the CI harness: a local Git repository, bare remote,
recording `gh` stub, and stub tidy binary.

Asserts the publication wiring end to end:

- a successful apply push publishes the run document at the pushed
  revision, with no change-report flood on first publication;
- a failed push and a check-mode failure publish the head-view document
  at the PR head revision, never the mutated working tree;
- a clean-tree PR run publishes advisory hints at HEAD;
- non-PR and validate runs publish nothing.
"""

import json
import os
import re
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

# Tidy stub: the mutating mode rewrites lib.rs to the tidy form and
# reports a mutated-tree finding; --dry-run (the head view) reports the
# file's committed first line without touching anything.
#
# Distinct messages identify whether a report used local or committed code.
TIDY_STUB = """#!/usr/bin/env bash
for a in "$@"; do [ "$a" = "--dry-run" ] && dry=1; done
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
TIDY_HINT_STUB = """#!/usr/bin/env bash
printf '[{"path":"lib.rs","line":1,"severity":"hint","code":"DOC999","message":"consider pre-allocating the buffer","item_kind":"fn","item_name":"hello","title":"advisory follow-up"}]\\n'
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


def extract_run_script(action_yml):
    """The `run: |` block of the composite step with id `tidy`."""
    lines = action_yml.read_text().splitlines()
    step = next(i for i, line in enumerate(lines)
                if re.match(r"\s*id:\s*tidy\s*$", line))
    start = next(i for i in range(step, len(lines))
                 if re.match(r"\s*run:\s*\|\s*$", lines[i])) + 1
    body = []
    for line in lines[start:]:
        if line.strip() and not line.startswith("        "):
            break
        body.append(line[8:])
    return "\n".join(body)


class RunScriptTests(unittest.TestCase):
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

        # The main run's argv, normally written by the args step.
        (self.temp / "rlt-args").write_bytes(b"--output-mode\0json\0.\0")

        script = extract_run_script(ACTION_DIR / "action.yml")
        # Resolve the runtime template the way `uses:` does: action_path is
        # the action root, and the script appends scripts/ itself.
        script = script.replace("${{ github.action_path }}", str(ACTION_DIR))
        self.run_script = self.root / "run.sh"
        self.run_script.write_text(script)

    def tearDown(self):
        self._tmp.cleanup()

    def git(self, *argv):
        """One real-git call in the fixture repository."""
        return subprocess.run(
            [self.real_git, "-C", str(self.repo), *argv],
            capture_output=True, text=True, check=True,
        )

    def run_action(self, **env):
        """Run the extracted script; extra kwargs override the env."""
        script_env = dict(
            os.environ,
            PATH=f"{self.bin_dir}:{os.environ['PATH']}",
            RUNNER_TEMP=str(self.temp),
            MODE="apply", IS_PR="true", PR_NUMBER="7", HEAD_REF="feature",
            COMMIT_NAME="bot", COMMIT_EMAIL="b@b", GITHUB_TOKEN="x",
            RLT_BIN=str(self.tidy), RLT_VALIDATE="false",
            GITHUB_REPOSITORY="o/r", GITHUB_SERVER_URL=SERVER,
        )
        script_env.pop("RLT_HEAD_SHA", None)
        script_env.pop("RLT_PUSH_FAIL", None)
        script_env.pop("RUST_LLM_TIDY_DIFF_BASE", None)
        script_env.pop("RLT_PR_BASE", None)
        script_env.update(env)
        return subprocess.run(
            ["bash", str(self.run_script)], cwd=self.repo, env=script_env,
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

    def test_successful_apply_push_publishes_at_the_pushed_revision(self):
        head_sha = self.untidy_commit_on_feature()
        # The real action always exports the pre-push PR head; the report
        # must still describe the pushed revision, never that stale head.
        result = self.run_action(RLT_HEAD_SHA=head_sha)
        self.assertEqual(result.returncode, 0, result.stderr)

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
        result = self.run_action(MODE="check", RLT_HEAD_SHA=head_sha)
        self.assertEqual(result.returncode, 1, result.stdout)

        self.assertIn("not tidy", self.pr_log.read_text())
        self.assertEqual(len(self.api_posts()), 1)
        body = self.posted_body()
        self.assertIn(f"blob/{head_sha}/lib.rs#L1", body)
        state = sticky_publish.decode_state(body)
        self.assertEqual(state["findings"][0]["record"]["message"],
                         "committed: fn b() { a(); }")

    def test_clean_pr_run_publishes_hints_at_head(self):
        result = self.run_action(RLT_BIN=str(self.tidy_hint))
        self.assertEqual(result.returncode, 0, result.stderr)

        self.assertEqual(len(self.api_posts()), 1)
        body = self.posted_body()
        self.assertIn("### Hints - consider looking at these", body)
        self.assertIn("1 hint.", body)
        state = sticky_publish.decode_state(body)
        self.assertEqual(state["findings"][0]["record"]["severity"], "hint")

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
        # An invalid repository drives sticky_publish.py to its usage
        # error; the wiring must warn and keep the tidy exit status.
        result = self.run_action(RLT_BIN=str(self.tidy_hint),
                                 GITHUB_REPOSITORY="not a repo")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("could not publish the findings report", result.stderr)

    def test_run_should_supply_baseline_when_pr_context_is_available(self):
        baseline_log = self.temp / "baseline"
        write_stub(self.tidy_hint, '''#!/usr/bin/env bash
printf '%s' "${RUST_LLM_TIDY_DIFF_BASE-unset}" > "$RUNNER_TEMP/baseline"
printf '[]\n'
''')
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

    def test_head_recollection_should_inherit_pr_baseline_when_check_fails(self):
        head_sha = self.untidy_commit_on_feature()
        script = TIDY_STUB.replace(
            'for a in "$@";',
            'printf "%s\\n" "${RUST_LLM_TIDY_DIFF_BASE-unset}" '
            '>> "$RUNNER_TEMP/baselines"\nfor a in "$@";',
        )
        write_stub(self.tidy, script)

        result = self.run_action(MODE="check", RLT_HEAD_SHA=head_sha,
                                 RLT_PR_BASE=head_sha)

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual((self.temp / "baselines").read_text().splitlines(),
                         [head_sha, head_sha])


if __name__ == "__main__":
    unittest.main()

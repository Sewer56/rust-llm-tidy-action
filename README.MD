# rust-llm-tidy GitHub Action

GitHub Action that runs [`rust-llm-tidy`][rust-llm-tidy] on Rust and Markdown,
and other files. Usable as a general-purpose action.

## What it does

Runs [`rust-llm-tidy`][rust-llm-tidy] (reorder, vis, markdown fixes, lints)
to gate pull requests.

Use cases:

- **PR review** - tidy changed files, fail if not tidy. Binary from a prebuilt
  release, or built from source via the vendored submodule layout.
- **Whole-tree pass** - tidy the whole `rust-project-path`.

How a binary is obtained:

1. **`prebuilt`** (default) - downloads a prebuilt release artifact (see
   [Releases](#releases)).
2. **`git`** - builds `rust-llm-tidy-cli` from a source checkout: the current
   repository by default (the vendored submodule layout), or a pinned remote
   checkout.
3. **`none`** - uses a `rust-llm-tidy` already on `PATH`.

Processing targets, in order of precedence: explicit `paths`, else the PR's
changed files (`changed-files: true`), else the whole `rust-project-path`.

The repository's own [`.rust-llm-tidy.yml`][config] config is auto-discovered
(walking up from `rust-project-path` to the repo root), for both modes.

- **`mode: apply`** (default, recommended) - tidies in place; on a PR commits
  and pushes the fixes to the PR branch. Fails if the push is impossible
  (e.g. fork PR).
- **`mode: check`** - tidies the target files, then fails if the tree is
  dirty, catching untidy *committed* PR content.

On any failure a PR comment listing the offending/fixed files is posted.

## Usage

Full example - vendored submodule, prebuilt binary, whole-repo apply on PRs:

```yaml
name: Tidy

on:
  pull_request:
    paths: ["src/**", "**/*.md", ".rust-llm-tidy.yml"]
  workflow_dispatch:

jobs:
  tidy:
    runs-on: ubuntu-latest
    permissions:
      contents: write         # apply: push fixes to the PR branch
      pull-requests: write
    steps:
      - uses: actions/checkout@v7
        with:
          ref: ${{ github.head_ref }}   # PR head, not merge ref (apply pushes here)
          submodules: recursive
      - uses: ./submodules/action
        with:
          binary-source: prebuilt   # or 'git' to build from this repo's source
          mode: apply               # commit + push fixes to the PR branch
          changed-files: "false"    # whole repo
```

`mode: apply` commits + pushes fixes itself (`contents: write`); keep
must-stay-dirty files under `.rust-llm-tidy.yml` `exclude_files`.

### Apply (recommended)

- Tidies in place.
- On a PR: commits + pushes fixes to the PR branch.
- Push impossible (fork PR, read-only token): fails.
- Outside a PR: leaves the changes in the working tree.

Fork PRs: the default token can't push the fork branch. Use `mode: check`
(comment + fail, no push) or a write-enabled token (`pull_request_target` +
PAT).

Example: run only `reorder` + `lints` on `src` and `README.md`, apply in
place:

```yaml
- uses: Sewer56/rust-llm-tidy-action@v1
  with:
    rust-project-path: '.'
    paths: |
      src
      README.md
    include: 'reorder,lints'
    mode: 'apply'
```

On success, `apply` pushes with the message
`Apply rust-llm-tidy fixes`, and posts a PR comment confirming the fix and the
pushed commit.

Both modes post a PR comment on failure, listing the offending/fixed files. To
allow those comments (and `apply`'s push to a same-repo PR branch), grant the
job `contents: write` and `pull-requests: write`. `check` needs
`pull-requests: write` to comment.

### PR check

Check-mode use: review a pull request in any consuming repository. Only the
files the PR changes are evaluated, and the job fails if any are not tidy:

```yaml
steps:
  - uses: actions/checkout@v6
    with:
      fetch-depth: 0   # full history so the PR base diff is resolvable

  - uses: Sewer56/rust-llm-tidy-action@v1
    with:
      rust-project-path: '.'
      changed-files: 'true'
      mode: 'check'
```

If the repository has no config file, the action still runs all ops on the
changed files. For a config-free gate, pass `no-config: 'true'` (or rely on
default config discovery, which finds nothing and runs everything).

### Whole-tree check

Without `changed-files`, the action tidies the whole `rust-project-path`:

```yaml
- uses: Sewer56/rust-llm-tidy-action@v1
  with:
    rust-project-path: '.'
    paths: '.'
    mode: 'check'
```

> Empty `paths` with `changed-files: false` tidies the whole
> `rust-project-path`. With `changed-files: true` it tidies only the files
> changed by the PR. Explicit `paths` always win over both.

### Build from source

Build the `rust-llm-tidy` binary from a source checkout instead of downloading
a release.

Default builds the current checkout at `${{ github.workspace }}`. To build a
pinned remote checkout instead, use `use-source-checkout: true`:

```yaml
- uses: Sewer56/rust-llm-tidy-action@v1
  with:
    binary-source: 'git'
    use-source-checkout: 'true'   # checkout Sewer56/rust-llm-tidy@<ref>
    source-repo-ref: '<SHA or tag>'
    rust-project-path: '.'
    paths: '.'
    mode: 'check'
```

If the current repository is not a `rust-llm-tidy` checkout, point
`source-path` at a local checkout or use `use-source-checkout: true`.

## Releases

For `binary-source: prebuilt`, the action looks for release assets named
`rust-llm-tidy-<target>` (plain binary) or `rust-llm-tidy-<target>.<ext>`
with `.7z`, `.zip`, `.tar.gz`, `.tar.xz`, `.tar.bz2`, `.tar.zst` or `.tar`.

`<target>` may be a full Rust triple (`x86_64-unknown-linux-gnu`) or the
compact form used by [Sewer56/rust-llm-tidy][release-repo] releases:

| Compact form  | Rust triple                |
| ------------- | -------------------------- |
| `linux-x64`   | `x86_64-unknown-linux-gnu` |
| `linux-x86`   | `i686-unknown-linux-gnu`   |
| `macos-arm64` | `aarch64-apple-darwin`     |
| `macos-x64`   | `x86_64-apple-darwin`      |
| `windows-x64` | `x86_64-pc-windows-msvc`   |
| `windows-x86` | `i686-pc-windows-msvc`     |

Archives extract to a single `rust-llm-tidy` / `rust-llm-tidy.exe`.

### Extraction with native 7-Zip

The action uses the **native 7-Zip CLI** (`7z`/`7zz`) to extract release
archives. How the 7-Zip binary is found depends on `sevenzip-source`:

| Value            | Behaviour                                                                                                                                                                 |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `auto` (default) | Use `7z`/`7zz` already on the runner (incl. Windows `Program Files\7-Zip`, macOS libarchive `tar`); otherwise download a standalone 7-Zip from [ip7z/7zip][7zip] releases |
| `fetch`          | Always download a standalone 7-Zip ([ip7z/7zip][7zip] releases)                                                                                                           |
| `version:<x.y>`  | Download a specific 7-Zip version, e.g. `version:26.02`                                                                                                                   |
| `apt`            | Linux only: require `p7zip-full` already installed (no download)                                                                                                          |
| `preinstalled`   | Require `7z`/`7zz` on the runner (no download)                                                                                                                            |

A downloaded 7-Zip is cached in `$RUNNER_TEMP/sevenzip-bin`; enable
`cache-sevenzip: true` to cache it across runs.

## Inputs

### Binary acquisition

| Input            | Description                                          | Default                 |
| ---------------- | ---------------------------------------------------- | ----------------------- |
| `binary-source`  | `prebuilt` / `git` / `none`                          | `prebuilt`              |
| `release-repo`   | Repo hosting prebuilt release (`owner/name`)         | `Sewer56/rust-llm-tidy` |
| `release-tag`    | Release tag to download (empty = latest)             | `''`                    |
| `download-asset` | Exact asset name (overrides target detection)        | `''`                    |
| `install-dir`    | Install dir (relative resolves under `$RUNNER_TEMP`) | `rust-llm-tidy-bin`     |
| `cache-prebuilt` | Cache the downloaded binary                          | `false`                 |

### 7-Zip extraction

| Input             | Description                                                                           | Default |
| ----------------- | ------------------------------------------------------------------------------------- | ------- |
| `sevenzip-source` | Where the native 7-Zip comes from (`auto`/`fetch`/`version:<v>`/`apt`/`preinstalled`) | `auto`  |
| `cache-sevenzip`  | Cache a downloaded standalone 7-Zip                                                   | `false` |

### Source build (git mode)

| Input                    | Description                                                                | Default                   |
| ------------------------ | -------------------------------------------------------------------------- | ------------------------- |
| `source-path`            | Path to a rust-llm-tidy source checkout (`<path>/src/Cargo.toml` expected) | `${{ github.workspace }}` |
| `use-source-checkout`    | Checkout `source-repo@source-repo-ref` into the workspace                  | `false`                   |
| `source-repo`            | Source repo for `use-source-checkout`                                      | `Sewer56/rust-llm-tidy`   |
| `source-repo-ref`        | Branch/tag/SHA to checkout (pin a SHA for reproducibility)                 | `main`                    |
| `install-rust-toolchain` | Install Rust toolchain for `git` build                                     | `true`                    |
| `rust-toolchain`         | Toolchain for `git` build                                                  | `stable`                  |
| `setup-rust-cache`       | Restore/save rust cache for `git` build                                    | `true`                    |

### Tidy invocation

| Input               | Description                                                             | Default                   |
| ------------------- | ----------------------------------------------------------------------- | ------------------------- |
| `rust-project-path` | Working directory for tidy run and git diff check                       | `${{ github.workspace }}` |
| `paths`             | Paths to tidy (one per line). Empty + `changed-files: false` tidies `.` | `''`                      |
| `changed-files`     | Empty `paths`: tidy only files changed by this PR (PR base...HEAD diff) | `false`                   |
| `mode`              | `apply` or `check`                                                      | `apply`                   |
| `config-path`       | Explicit `.rust-llm-tidy.yml` path (`--config`)                         | `''`                      |
| `no-config`         | Disable config discovery (`--no-config`)                                | `false`                   |
| `include`           | Ops/lint-codes to run (`--include`), e.g. `reorder,vis`                 | `''`                      |
| `exclude`           | Ops/lint-codes to skip (`--exclude`), e.g. `lints`                      | `''`                      |
| `dry-run`           | Print results instead of modifying files                                | `false`                   |
| `validate`          | Validate config and exit                                                | `false`                   |

## Outputs

| Output            | Description                                      |
| ----------------- | ------------------------------------------------ |
| `binary-path`     | Path to the `rust-llm-tidy` binary that was used |
| `source-dir`      | Resolved source directory (`git` mode only)      |
| `sevenzip-binary` | Path to the 7-Zip binary used                    |

### PR comments

The action replies on the PR in one of five ways:

**1. All tidy** - nothing to change, `check` or `apply` pass:

```markdown
## rust-llm-tidy: :white_check_mark: all tidy

All files are tidy - no changes required.
```

**2. Changes** - green, but the run recorded changes it would make (`dry-run`)
or that fall short of a push; shows a "Changes" block. Table and link fixes
carry no line number, shown as `-`:

```markdown
## rust-llm-tidy: changes

### Changes
| File          | Line | Code      | Change                                                        |
| ------------- | ---- | --------- | ------------------------------------------------------------- |
| `README.md`   | -    | `FIX`     | tables were aligned                                           |
| `src/main.rs` | 5    | `VIS`     | narrow visibility of `helper` at line 5                       |
| `src/main.rs` | 9    | `REORDER` | rearrange fn `a_main` from pos 3 to pos 1 (before `b_helper`) |
| `README.md`   | 12   | `FIX`     | flip nested fence at line 12                                  |
```

**3. Not tidy** - lint findings, job fails. Renders a lint table; if the run
yielded nothing table-shaped it falls back to a plain file list:

```markdown
## rust-llm-tidy: not tidy

| File          | Line | Severity | Code     | Reason                                  |
| ------------- | ---- | -------- | -------- | --------------------------------------- |
| `src/main.rs` | 12   | error    | `DOC002` | Missing ## Errors section               |
| `src/lib.rs`  | 44   | warning  | `TEST001`| Test fn lacks #[test] attribute         |
```

**4. Fixes applied** - `mode: apply` pushed the fix commit to the PR branch:

```markdown
## rust-llm-tidy: :white_check_mark: fixes applied

I tidied the files below and pushed commit `abc1234`.

Changed files:
  - `src/main.rs`
  - `README.md`
```

**5. Could not push fixes** - `mode: apply` tidied but could not push (fork PR
or token without write access). Job fails:

```markdown
## rust-llm-tidy: could not push fixes

I tidied the files below but could not push the fix commit (fork PR or the
token lacks write access to the PR branch).

Fixed files:
  - `src/main.rs`

To get the fixes applied, re-run with a token that can write to the PR branch
(e.g. `permissions: contents: write`, or a PAT on fork PRs via
`pull_request_target`).
```

## License

Apache-2.0, matching [`rust-llm-tidy`][rust-llm-tidy].

[rust-llm-tidy]: https://github.com/Sewer56/rust-llm-tidy
[release-repo]: https://github.com/Sewer56/rust-llm-tidy/releases
[config]: https://github.com/Sewer56/rust-llm-tidy#configuration
[7zip]: https://github.com/ip7z/7zip

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

**`mode: check`** (recommended for PRs) tidies the target files and then fails
the job if the tree is dirty, so untidy *committed* PR content is caught.
**`mode: apply`** tidies in place, and on a pull request also commits and
pushes the fixes back to the PR branch. If that push is impossible (e.g. fork
PRs, where a plain `GITHUB_TOKEN` cannot write the fork's branch) it posts a
PR comment listing the fixed files and fails. On any failure a PR comment
listing the offending/fixed files is posted.

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

### PR check (recommended)

The headline use: review a pull request in any consuming repository. The
repository's own [`.rust-llm-tidy.yml`][config] is auto-discovered (walking up
from `rust-project-path` to the repo root), only the files the PR changes are
evaluated, and the job fails if any are not tidy:

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

### Apply (auto-fix)

`mode: apply` mutates in place and, on a pull request, commits + pushes the
fixes to the PR branch (for same-repo PRs; the default GITHUB_TOKEN has write
there). If the push is not possible (fork PR, or a token without write) it
posts a PR comment listing the fixed files and fails instead of silently
editing. Outside a pull request, `apply` just mutates the working tree and
leaves the changes for you to commit.

> For fork PRs, `apply` cannot push with the default token. Either use
> `mode: check` (comment + fail, no push) or grant a token that can write the
> fork's branch (e.g. a PAT via `pull_request_target`).

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

> `check` is a gate only - it never commits. Use it when you want to block an
> untidy PR rather than auto-fix it.

### Build from source (git mode - current repository)

`git` mode defaults `source-path` to the current repository checkout
(`${{ github.workspace }}`). Vendored as a submodule at
`<repo>/submodules/action`, that default is the enclosing `rust-llm-tidy`
checkout at its current commit - no hardcoded parent path:

```yaml
- uses: ./submodules/action
  with:
    binary-source: 'git'   # default source-path = current repository
    # source-path: '${{ github.workspace }}'   # (the default, shown for clarity)
    rust-project-path: '.'
    paths: '.'
    mode: 'check'
```

`paths: '.'` tidies the whole repository. If the current repository is not a
`rust-llm-tidy` checkout, set `source-path` explicitly or use
`use-source-checkout: true`.

### Build from a pinned remote checkout

```yaml
- uses: Sewer56/rust-llm-tidy-action@v1
  with:
    binary-source: 'git'
    use-source-checkout: 'true'   # checkout Sewer56/rust-llm-tidy@<ref> into the workspace
    source-repo-ref: '<SHA or tag>'
    rust-project-path: '.'
    paths: '.'
    mode: 'check'
```

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

| Value            | Behaviour                                                                                                                                                                                         |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `auto` (default) | Use `7z`/`7zz` already on the runner (incl. Windows `Program Files\7-Zip`, macOS libarchive `tar`); otherwise download a standalone 7-Zip from [ip7z/7zip][7zip] releases                         |
| `fetch`          | Always download a standalone 7-Zip ([ip7z/7zip][7zip] releases)                                                                                                                                   |
| `version:<x.y>`  | Download a specific 7-Zip version, e.g. `version:26.02`                                                                                                                                           |
| `apt`            | Linux only: require `p7zip-full` already installed (no download)                                                                                                                                  |
| `preinstalled`   | Require `7z`/`7zz` on the runner (no download)                                                                                                                                                    |

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

| Input               | Description                                                                  | Default                   |
| ------------------- | ---------------------------------------------------------------------------- | ------------------------- |
| `rust-project-path` | Working directory for tidy run and git diff check                            | `${{ github.workspace }}` |
| `paths`             | Paths to tidy (one per line). Empty + `changed-files: false` tidies `.`      | `''`                      |
| `changed-files`     | Empty `paths`: tidy only files changed by this PR (PR base...HEAD diff)      | `false`                   |
| `mode`              | `apply` or `check`                                                           | `apply`                   |
| `config-path`       | Explicit `.rust-llm-tidy.yml` path (`--config`)                              | `''`                      |
| `no-config`         | Disable config discovery (`--no-config`)                                     | `false`                   |
| `include`           | Ops/lint-codes to run (`--include`), e.g. `reorder,vis`                      | `''`                      |
| `exclude`           | Ops/lint-codes to skip (`--exclude`), e.g. `lints`                           | `''`                      |
| `dry-run`           | Print results instead of modifying files                                     | `false`                   |
| `validate`          | Validate config and exit                                                     | `false`                   |

## Outputs

| Output            | Description                                      |
| ----------------- | ------------------------------------------------ |
| `binary-path`     | Path to the `rust-llm-tidy` binary that was used |
| `source-dir`      | Resolved source directory (`git` mode only)      |
| `sevenzip-binary` | Path to the 7-Zip binary used                    |

## License

Apache-2.0, matching [`rust-llm-tidy`][rust-llm-tidy].

[rust-llm-tidy]: https://github.com/Sewer56/rust-llm-tidy
[release-repo]: https://github.com/Sewer56/rust-llm-tidy/releases
[config]: https://github.com/Sewer56/rust-llm-tidy#configuration
[7zip]: https://github.com/ip7z/7zip

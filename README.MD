# rust-llm-tidy GitHub Action

GitHub Action that runs [`rust-llm-tidy`][rust-llm-tidy] (fix, reorder,
narrow visibility, lint) on Rust and Markdown files. Intended for the project's
own CI (vendored as a submodule) and as a general-purpose action.

## What it does

- Gets a `rust-llm-tidy` binary one of three ways:
  1. **`prebuilt`** (default) - downloads a prebuilt release artifact from a
     GitHub release (`.7z` / `.tar.gz` / `.zip` / plain binary).
  2. **`git`** - builds `rust-llm-tidy-cli` from a source checkout, either the
     enclosing repository (submodule layout) or a pinned remote checkout.
  3. **`none`** - uses a `rust-llm-tidy` already on `PATH`.
- Runs it against your Rust/Markdown paths, or (with no `paths`) against the
  changed files in the current git diff.
- **`mode: check`** fails the job if the working tree is not tidy (mutation is
  left in place for inspection). **`mode: apply`** (default) just tidies.

## Usage

```yaml
steps:
  - uses: actions/checkout@v6

  # Apply tidy, then reorder+lint only, using the prebuilt release binary.
  - uses: Sewer56/rust-llm-tidy-action@v1
    with:
      rust-project-path: '.'
      paths: |
        src
        README.md
      include: 'reorder,lints'
      mode: 'apply'
```

### Check mode in CI

```yaml
- name: Tidy check
  uses: Sewer56/rust-llm-tidy-action@v1
  with:
    rust-project-path: '.'
    include: 'reorder,vis,fix'
    mode: 'check'
```

### Build from source (submodule layout - current commit)

When the action is vendored as a submodule at `<repo>/submodules/action`, the
default `source-path: ${{ github.action_path }}/..` resolves to the enclosing
`rust-llm-tidy` checkout **at the current commit**. Use this to test/tidy the
repository against its own current source:

```yaml
- uses: ./submodules/action
  with:
    binary-source: 'git'          # default source-path = enclosing checkout
    rust-project-path: '.'
    mode: 'check'
```

### Build from a pinned remote checkout

```yaml
- uses: Sewer56/rust-llm-tidy-action@v1
  with:
    binary-source: 'git'
    use-source-checkout: 'true'   # checkout Sewer56/rust-llm-tidy@<ref> into $RUNNER_TEMP
    source-repo-ref: '<SHA or tag>'
    rust-project-path: '.'
    mode: 'check'
```

## Releases

For `binary-source: prebuilt`, the action looks for release assets named
`rust-llm-tidy-<target>` (plain binary) or `rust-llm-tidy-<target>.<ext>`
with `.7z`, `.tar.gz`, `.zip` or `.tar.xz`. `<target>` may be a full Rust
triple (`x86_64-unknown-linux-gnu`) or the compact form used by
[Sewer56/rust-llm-tidy][release-repo] releases (`linux-x64`, `linux-x86`,
`macos-arm64`, `macos-x64`, `windows-x64`, `windows-x86`). Archives extract to
a single `rust-llm-tidy`/`rust-llm-tidy.exe`.

`7z` extraction: Windows runners include 7-Zip, macOS `tar` is libarchive, and
Linux installs `p7zip-full` automatically (with a pure-Python `py7zr` fallback).

## Inputs

| Input                  | Description                                                                 | Default                              |
| ---------------------- | --------------------------------------------------------------------------- | ------------------------------------ |
| `binary-source`        | `prebuilt` / `git` / `none`                                                  | `prebuilt`                           |
| `release-repo`         | Repo hosting prebuilt release (`owner/name`)                                 | `Sewer56/rust-llm-tidy`              |
| `release-tag`          | Release tag to download (empty = latest)                                     | `''`                                 |
| `download-asset`       | Exact asset name (overrides target detection)                                | `''`                                 |
| `install-dir`          | Install dir (relative resolves under `$RUNNER_TEMP`)                         | `rust-llm-tidy-bin`                  |
| `cache-prebuilt`       | Cache the downloaded binary                                                  | `false`                              |
| `source-path`          | Path to a rust-llm-tidy source checkout (`<path>/src/Cargo.toml` expected)   | `${{ github.action_path }}/..`       |
| `use-source-checkout`  | Checkout `source-repo@source-repo-ref` into `$RUNNER_TEMP`                   | `false`                              |
| `source-repo`          | Source repo for `use-source-checkout`                                        | `Sewer56/rust-llm-tidy`              |
| `source-repo-ref`      | Branch/tag/SHA to checkout (pin a SHA for reproducibility)                  | `main`                               |
| `install-rust-toolchain`| Install Rust toolchain for `git` build                                       | `true`                               |
| `rust-toolchain`       | Toolchain for `git` build                                                    | `stable`                             |
| `setup-rust-cache`     | Restore/save rust cache for `git` build                                      | `true`                               |
| `rust-project-path`    | Working directory for tidy run and git diff check                            | `${{ github.workspace }}`            |
| `paths`                | Paths to tidy (one per line; empty = git diff changed files)                 | `''`                                 |
| `mode`                 | `apply` or `check`                                                           | `apply`                              |
| `config-path`          | Explicit `.rust-llm-tidy.yml` path (`--config`)                              | `''`                                 |
| `no-config`            | Disable config discovery (`--no-config`)                                     | `false`                              |
| `include`              | Ops/lint-codes to run (`--include`), e.g. `reorder,vis`                      | `''`                                 |
| `exclude`              | Ops/lint-codes to skip (`--exclude`), e.g. `lints`                           | `''`                                 |
| `dry-run`              | Print results instead of modifying files                                     | `false`                              |
| `validate`             | Validate config and exit                                                    | `false`                              |

## Outputs

| Output        | Description                                          |
| ------------- | ---------------------------------------------------- |
| `binary-path` | Path to the `rust-llm-tidy` binary that was used    |
| `source-dir`  | Resolved source directory (`git` mode only)          |

## Development

Run the action's own test workflow (`.github/workflows/test-action.yml`), or
test locally:

```bash
# dry-run tidy the current repo's src using a source build
cd <rust-llm-tidy checkout>
python3 submodules/action/scripts/resolve_artifact.py   # prebuilt path
cargo build --release -p rust-llm-tidy-cli --bin rust-llm-tidy --manifest-path ./src/Cargo.toml
./src/target/release/rust-llm-tidy --validate
```

## License

Apache-2.0, matching [`rust-llm-tidy`][rust-llm-tidy].

[rust-llm-tidy]: https://github.com/Sewer56/rust-llm-tidy
[release-repo]: https://github.com/Sewer56/rust-llm-tidy/releases

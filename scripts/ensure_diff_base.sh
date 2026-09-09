#!/usr/bin/env bash
# Make a baseline's merge-base with HEAD available without changing the checkout.
set -euo pipefail

base="$1"
if ! git rev-parse --verify --quiet --end-of-options "$base^{commit}" >/dev/null; then
  if ! git fetch --no-tags origin "$base"; then
    echo "::error::rust-llm-tidy: could not fetch diff baseline $base; check origin access" >&2
    exit 1
  fi
fi

if git merge-base "$base" HEAD >/dev/null; then
  exit 0
fi

if [ "$(git rev-parse --is-shallow-repository)" = "true" ]; then
  echo "rust-llm-tidy: fetching history to resolve the diff baseline"
  # Explicit endpoints also cover checkouts whose fetch refspec excludes the PR.
  if ! git fetch --no-tags --unshallow origin "$base" "$(git rev-parse HEAD)"; then
    echo "::error::rust-llm-tidy: could not fetch diff history; check origin access" >&2
    exit 1
  fi
fi

if ! git merge-base "$base" HEAD >/dev/null; then
  echo "::error::rust-llm-tidy: diff baseline $base has no merge-base with HEAD; use a related baseline" >&2
  exit 1
fi

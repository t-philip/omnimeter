#!/usr/bin/env bash
# Guard against tagging a release without bumping src/__version__.py first.
#
# Why this exists: __version__.py's own header already documented the rule
# ("bump this in the same commit... before tagging... never after") but nothing
# enforced it -- v1.0.1 through v1.0.4 were all tagged and released while the
# constant silently stayed at 1.0.0, so the running app's footer/GET
# /api/version drifted 4 releases behind reality until someone noticed.
#
# Run before every `gh release create vX.Y.Z`:
#
#   scripts/check_version.sh v1.0.6
set -euo pipefail

if [ $# -ne 1 ]; then
    echo "usage: $0 vX.Y.Z" >&2
    exit 2
fi

tag="$1"
expected="${tag#v}"
actual=$(grep -oP '__version__ = "\K[^"]+' src/__version__.py)

if [ "$actual" != "$expected" ]; then
    echo "ERROR: src/__version__.py says \"$actual\", but you're about to tag \"$tag\"." >&2
    echo "Bump src/__version__.py to \"$expected\" and commit it BEFORE tagging." >&2
    exit 1
fi

echo "OK: src/__version__.py ($actual) matches tag $tag"

#!/usr/bin/env bash
# Guard against tagging an android-vX.Y.Z release without bumping
# android/app/build.gradle.kts's versionName first.
#
# Why this exists: the Android app is versioned and released separately from
# the main web app (see android/README.md) specifically because it's touched
# far less often -- which is exactly the situation that lets a version
# constant silently drift, the same class of bug check_version.sh already
# guards against for the web app.
#
# Run before every `gh release create android-vX.Y.Z`:
#
#   scripts/check_android_version.sh android-v1.0.1
set -euo pipefail

if [ $# -ne 1 ]; then
    echo "usage: $0 android-vX.Y.Z" >&2
    exit 2
fi

tag="$1"
expected="${tag#android-v}"
actual=$(grep -oP 'versionName = "\K[^"]+' android/app/build.gradle.kts)

if [ "$actual" != "$expected" ]; then
    echo "ERROR: android/app/build.gradle.kts's versionName says \"$actual\", but you're about to tag \"$tag\"." >&2
    echo "Bump versionName (and versionCode) in android/app/build.gradle.kts and commit it BEFORE tagging." >&2
    exit 1
fi

echo "OK: android/app/build.gradle.kts versionName ($actual) matches tag $tag"

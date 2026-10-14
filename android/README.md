# OmniMeter Android app

A native Kotlin WebView shell around your own self-hosted OmniMeter dashboard —
full-screen, its own launcher icon, no browser chrome. It loads your instance's
web UI remotely rather than bundling any of its own frontend, so any change you
pull into your OmniMeter install (see the main [README](../README.md)'s
"Upgrading") reaches the app automatically with no rebuild needed.

Package: `eu.tphilip.omnimeter`.

**Requires an OmniMeter instance already running and reachable from your
phone.** This app is a client only — it doesn't set one up for you. If you
don't have one yet, see the main [README](../README.md)'s Quick Start first.

This app is versioned and released separately from the main OmniMeter web
app — look for a release tagged `android-vX.Y.Z`, not the repo's regular
`vX.Y.Z` releases (those are the web app; GitHub's "Latest release" badge
usually points there, not here).

## Getting it

**Download a build:** the [latest Android release](https://github.com/t-philip/omnimeter/releases/tag/android-v1.0.0)
has a debug-signed APK attached (check the
[full release list](https://github.com/t-philip/omnimeter/releases) for a
newer `android-v*` tag if one exists). Android will warn about installing
from an "unknown source" — expected for anything not distributed via the
Play Store; allow it for this app if you trust the download.

**Or build it yourself:**

```
cd android
./gradlew assembleDebug
```

Requires JDK 17+ and the Android SDK (`platforms;android-36`,
`build-tools;36.0.0`). The resulting APK lands at
`app/build/outputs/apk/debug/app-debug.apk` — install it via `adb install` or
by copying it to your device.

## First launch

The app has no default host baked in — every OmniMeter instance lives at a
different address. On first launch (or if it can't reach the address it has),
it opens Settings directly: enter your instance's scheme (`http`/`https`),
host, and port, and save.

**Cleartext HTTP works for any host you enter.** OmniMeter has no built-in
HTTPS by default (see the main README's "Deliberately not built" note), so
`http://` is the expected, normal case here. If you've put your own reverse
proxy with HTTPS in front of your instance, use `https://` instead.

## Play Store

Currently available only via APK download. Bringing it to the Google Play
Store is planned.

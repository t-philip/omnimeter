# OmniMeter Android app

A native Kotlin WebView shell around your own self-hosted OmniMeter dashboard —
full-screen, its own launcher icon, no browser chrome. It loads your instance's
web UI remotely rather than bundling any of its own frontend, so any change you
pull into your OmniMeter install (see the main [README](../README.md)'s
"Upgrading") reaches the app automatically with no rebuild needed.

Package: `eu.tphilip.omnimeter`.

## Getting it

**Download a build:** the latest [GitHub Release](https://github.com/t-philip/omnimeter/releases)
has a debug-signed APK attached. Android will warn about installing from an
"unknown source" — expected for anything not distributed via the Play Store;
allow it for this app if you trust the download.

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
`http://` is the expected, normal case here — not a restricted allowlist like
an earlier private build of this app used. If you've put your own reverse
proxy with HTTPS in front of your instance, use `https://` instead.

## Signing

Built as a debug APK (the standard Android SDK debug key) — fine for
sideloading, not intended for Play Store distribution as-is. There is no
release-signing keystore in this repo.

## Play Store

Not published there yet. A generic self-hosted-client WebView app like this
one is a real, common category on the Play Store (Home Assistant, Synology's
DS app, Nextcloud, and others all work this way), so it's a plausible future
step — but it needs its own separate effort (developer account, a hosted
privacy policy, Play's Data Safety declaration, and likely a proper
release-signed build rather than this debug one). Sideloading is the only
distribution path for now.

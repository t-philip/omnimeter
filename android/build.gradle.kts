plugins {
    id("com.android.application") version "8.13.0" apply false
    id("org.jetbrains.kotlin.android") version "2.0.21" apply false
}

// Build output redirected outside this project's own tree. If you've cloned this
// repo into a cloud-synced folder (OneDrive, Dropbox, Google Drive...), that
// sync client's real-time watcher can repeatedly lock files inside a
// rapidly-churning Gradle build/ directory, causing intermittent
// "Unable to delete directory" failures. Source stays tracked in the repo as
// normal -- only intermediate/output artefacts (already gitignored anyway) move.
// Resolved per-user/per-OS, not hardcoded to any one machine or username.
val localBuildRoot = System.getenv("LOCALAPPDATA")?.let { "$it/omnimeter-android-build" }
    ?: "${System.getProperty("user.home")}/.omnimeter-android-build"
rootProject.layout.buildDirectory.set(file("$localBuildRoot/root"))
subprojects {
    layout.buildDirectory.set(file("$localBuildRoot/${project.name}"))
}

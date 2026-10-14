package eu.tphilip.omnimeter

import android.content.Context
import android.content.SharedPreferences

class Prefs(context: Context) {

    private val prefs: SharedPreferences =
        context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    var scheme: String
        get() = prefs.getString(KEY_SCHEME, DEFAULT_SCHEME) ?: DEFAULT_SCHEME
        set(value) = prefs.edit().putString(KEY_SCHEME, value).apply()

    var host: String
        get() = prefs.getString(KEY_HOST, DEFAULT_HOST) ?: DEFAULT_HOST
        set(value) = prefs.edit().putString(KEY_HOST, value).apply()

    var port: Int
        get() = prefs.getInt(KEY_PORT, DEFAULT_PORT)
        set(value) = prefs.edit().putInt(KEY_PORT, value).apply()

    val baseUrl: String
        get() {
            val defaultPort = if (scheme == "https") 443 else 80
            return if (port == defaultPort) {
                "$scheme://$host"
            } else {
                "$scheme://$host:$port"
            }
        }

    // No default host makes sense across installs -- every OmniMeter instance
    // is a different self-hosted address. A blank host means "not configured
    // yet", not "use some guessed default"; MainActivity checks this to send
    // a first launch straight to Settings instead of loading a meaningless URL.
    val isConfigured: Boolean
        get() = host.isNotBlank()

    companion object {
        private const val PREFS_NAME = "omnimeter_prefs"
        private const val KEY_SCHEME = "scheme"
        private const val KEY_HOST = "host"
        private const val KEY_PORT = "port"

        const val DEFAULT_SCHEME = "http"
        const val DEFAULT_HOST = ""
        const val DEFAULT_PORT = 8000

        fun isValidHost(host: String): Boolean = host.isNotBlank()

        fun isValidPort(port: Int): Boolean = port in 1..65535
    }
}

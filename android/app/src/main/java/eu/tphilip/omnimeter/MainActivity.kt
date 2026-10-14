package eu.tphilip.omnimeter

import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.util.Log
import android.view.Menu
import android.view.MenuItem
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import eu.tphilip.omnimeter.databinding.ActivityMainBinding

private const val TAG = "OmniMeter"

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var prefs: Prefs
    private var loadedHost: String = ""

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        setSupportActionBar(binding.toolbar)

        prefs = Prefs(this)

        setupWebView()
        setupSwipeRefresh()
        setupBackHandling()
        setupErrorScreenButtons()
        loadDashboardIfConfigured()
    }

    override fun onResume() {
        super.onResume()
        // Covers two cases: Settings changed the host while this activity was
        // paused (loadedHost set, now stale), and first-run setup just
        // finished (loadedHost still empty, host now configured).
        if (prefs.isConfigured && loadedHost != prefs.baseUrl) {
            loadDashboard()
        }
    }

    // No host configured yet (fresh install) -- go straight to Settings
    // rather than trying to load a meaningless empty URL.
    private fun loadDashboardIfConfigured() {
        if (prefs.isConfigured) {
            loadDashboard()
        } else {
            startActivity(Intent(this, SettingsActivity::class.java))
        }
    }

    private fun setupWebView() {
        val webView = binding.webView
        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            useWideViewPort = true
            loadWithOverviewMode = true
            builtInZoomControls = true
            displayZoomControls = false
            allowFileAccess = false
            allowContentAccess = false
        }

        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(
                view: WebView,
                request: WebResourceRequest
            ): Boolean {
                val url = request.url
                Log.d(TAG, "shouldOverrideUrlLoading: $url (isForMainFrame=${request.isForMainFrame})")
                return if (url.host == Uri.parse(prefs.baseUrl).host) {
                    false // let the WebView handle it
                } else {
                    startActivity(Intent(Intent.ACTION_VIEW, url))
                    true
                }
            }

            override fun onReceivedError(
                view: WebView,
                request: WebResourceRequest,
                error: WebResourceError
            ) {
                Log.d(TAG, "onReceivedError: url=${request.url} isForMainFrame=${request.isForMainFrame} errorCode=${error.errorCode} description=${error.description}")
                if (request.isForMainFrame) {
                    showError(request.url.toString(), error.description?.toString())
                }
            }

            override fun onReceivedHttpError(
                view: WebView,
                request: WebResourceRequest,
                errorResponse: WebResourceResponse
            ) {
                Log.d(TAG, "onReceivedHttpError: url=${request.url} isForMainFrame=${request.isForMainFrame} status=${errorResponse.statusCode}")
                if (request.isForMainFrame) {
                    showError(request.url.toString(), "HTTP ${errorResponse.statusCode}")
                }
            }

            override fun onPageStarted(view: WebView, url: String, favicon: android.graphics.Bitmap?) {
                super.onPageStarted(view, url, favicon)
                Log.d(TAG, "onPageStarted: $url")
            }

            override fun onPageFinished(view: WebView, url: String) {
                super.onPageFinished(view, url)
                Log.d(TAG, "onPageFinished: $url")
                binding.swipeRefresh.isRefreshing = false
            }
        }

        webView.setOnScrollChangeListener { _, _, scrollY, _, _ ->
            binding.swipeRefresh.isEnabled = scrollY == 0
        }
    }

    private fun setupSwipeRefresh() {
        binding.swipeRefresh.setOnRefreshListener {
            binding.webView.reload()
        }
    }

    private fun setupBackHandling() {
        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (binding.webView.canGoBack()) {
                    binding.webView.goBack()
                } else {
                    isEnabled = false
                    onBackPressedDispatcher.onBackPressed()
                }
            }
        })
    }

    private fun loadDashboard() {
        loadedHost = prefs.baseUrl
        Log.d(TAG, "loadDashboard: loading $loadedHost")
        hideError()
        binding.webView.loadUrl(prefs.baseUrl)
    }

    private fun showError(url: String, detail: String?) {
        Log.d(TAG, "showError: url=$url detail=$detail")
        binding.swipeRefresh.visibility = android.view.View.GONE
        binding.errorLayout.visibility = android.view.View.VISIBLE
        binding.errorMessage.text = getString(R.string.error_message_generic)
        binding.errorUrl.text = if (detail != null) "$url\n($detail)" else url
    }

    private fun hideError() {
        binding.swipeRefresh.visibility = android.view.View.VISIBLE
        binding.errorLayout.visibility = android.view.View.GONE
    }

    private fun setupErrorScreenButtons() {
        binding.retryButton.setOnClickListener { loadDashboard() }
        binding.settingsFromErrorButton.setOnClickListener {
            startActivity(Intent(this, SettingsActivity::class.java))
        }
    }

    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menuInflater.inflate(R.menu.main_menu, menu)
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        return when (item.itemId) {
            R.id.action_settings -> {
                startActivity(Intent(this, SettingsActivity::class.java))
                true
            }
            else -> super.onOptionsItemSelected(item)
        }
    }
}

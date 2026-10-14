package eu.tphilip.omnimeter

import android.os.Bundle
import android.text.Editable
import android.text.TextWatcher
import android.widget.ArrayAdapter
import androidx.appcompat.app.AppCompatActivity
import eu.tphilip.omnimeter.databinding.ActivitySettingsBinding

class SettingsActivity : AppCompatActivity() {

    private lateinit var binding: ActivitySettingsBinding
    private lateinit var prefs: Prefs

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivitySettingsBinding.inflate(layoutInflater)
        setContentView(binding.root)
        setSupportActionBar(binding.toolbar)
        supportActionBar?.setDisplayHomeAsUpEnabled(true)
        binding.toolbar.setNavigationOnClickListener { onSupportNavigateUp() }

        prefs = Prefs(this)

        val schemeAdapter = ArrayAdapter(this, android.R.layout.simple_list_item_1, listOf("http", "https"))
        binding.schemeInput.setAdapter(schemeAdapter)
        binding.schemeInput.setText(prefs.scheme, false)
        binding.hostInput.setText(prefs.host)
        binding.portInput.setText(prefs.port.toString())

        updatePreview()

        val watcher = object : TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) {}
            override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) {}
            override fun afterTextChanged(s: Editable?) = updatePreview()
        }
        binding.hostInput.addTextChangedListener(watcher)
        binding.portInput.addTextChangedListener(watcher)
        binding.schemeInput.addTextChangedListener(watcher)

        binding.saveButton.setOnClickListener { save() }
        binding.cancelButton.setOnClickListener { finish() }
    }

    private fun currentScheme(): String = binding.schemeInput.text.toString().ifBlank { Prefs.DEFAULT_SCHEME }

    private fun updatePreview() {
        val scheme = currentScheme()
        val host = binding.hostInput.text.toString()
        val portText = binding.portInput.text.toString()
        val port = portText.toIntOrNull()
        val defaultPort = if (scheme == "https") 443 else 80
        val preview = if (host.isBlank() || port == null) {
            ""
        } else if (port == defaultPort) {
            "$scheme://$host"
        } else {
            "$scheme://$host:$port"
        }
        binding.baseUrlPreview.text = getString(R.string.settings_base_url_label) + " " + preview
    }

    private fun save() {
        val host = binding.hostInput.text.toString().trim()
        val port = binding.portInput.text.toString().toIntOrNull()

        binding.hostLayout.error = null
        binding.portLayout.error = null

        var valid = true
        if (!Prefs.isValidHost(host)) {
            binding.hostLayout.error = getString(R.string.settings_invalid_host)
            valid = false
        }
        if (port == null || !Prefs.isValidPort(port)) {
            binding.portLayout.error = getString(R.string.settings_invalid_port)
            valid = false
        }
        if (!valid) return

        prefs.scheme = currentScheme()
        prefs.host = host
        prefs.port = port!!
        finish()
    }
}

package com.hanstock.app

import android.content.Intent
import android.graphics.Bitmap
import android.os.Bundle
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.biometric.BiometricPrompt
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import androidx.fragment.app.FragmentActivity
import com.hanstock.app.bridge.WebAppInterface
import com.hanstock.app.ui.theme.HanstockTheme
import org.json.JSONObject

class MainActivity : FragmentActivity() {

    private var webView: WebView? = null
    
    // Default URL is configured through BuildConfig; the current backend runs on OCI.
    private val defaultUrl = "http://168.110.102.249:8000"

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        
        setContent {
            HanstockTheme {
                MainScreen()
            }
        }
    }

    @Composable
    fun MainScreen() {
        var isLoading by remember { mutableStateOf(true) }
        var showExitDialog by remember { mutableStateOf(false) }

        // Compose native back button handler
        BackHandler {
            if (webView?.canGoBack() == true) {
                webView?.goBack()
            } else {
                showExitDialog = true
            }
        }

        Box(
            modifier = Modifier
                .fillMaxSize()
                .background(MaterialTheme.colorScheme.background)
        ) {
            // Android WebView integrated via AndroidView
            AndroidView(
                modifier = Modifier.fillMaxSize(),
                factory = { context ->
                    WebView(context).apply {
                        webView = this
                        
                        // Setup WebView settings for maximum performance
                        settings.apply {
                            javaScriptEnabled = true
                            domStorageEnabled = true
                            databaseEnabled = true
                            cacheMode = WebSettings.LOAD_DEFAULT
                            mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
                            
                            // Responsive design support
                            useWideViewPort = true
                            loadWithOverviewMode = true
                            builtInZoomControls = true
                            displayZoomControls = false
                        }
                        
                        // JS native bridge registration
                        addJavascriptInterface(WebAppInterface(this@MainActivity), "androidApp")
                        
                        webViewClient = object : WebViewClient() {
                            override fun onPageStarted(view: WebView?, url: String?, favicon: Bitmap?) {
                                super.onPageStarted(view, url, favicon)
                                isLoading = true
                            }

                            override fun onPageFinished(view: WebView?, url: String?) {
                                super.onPageFinished(view, url)
                                isLoading = false
                                
                                // Check if there's any deep-link routing requested on initial load
                                handleDeepLinkIntent(intent)
                            }

                            override fun onReceivedError(
                                view: WebView?,
                                request: WebResourceRequest?,
                                error: WebResourceError?
                            ) {
                                super.onReceivedError(view, request, error)
                                // Add custom offline layout or retry button here if needed
                            }
                        }

                        webChromeClient = object : WebChromeClient() {
                            // Chrome Client for JS dialogs and console logs
                        }

                        loadUrl(defaultUrl)
                    }
                }
            )

            // Sleek Dark Gradient Loading Indicator Overlay
            if (isLoading) {
                Box(
                    modifier = Modifier
                        .fillMaxSize()
                        .background(
                            Brush.verticalGradient(
                                colors = listOf(
                                    Color(0xFF0A0F1D),
                                    Color(0xFF121829)
                                )
                            )
                        ),
                    contentAlignment = Alignment.Center
                ) {
                    Text(
                        text = "한스톡 테스트 화면",
                        color = MaterialTheme.colorScheme.primary
                    )
                }
            }

            // Compose Exit Dialog
            if (showExitDialog) {
                AlertDialog(
                    onDismissRequest = { showExitDialog = false },
                    title = { Text(text = getString(R.string.exit_title)) },
                    text = { Text(text = getString(R.string.exit_message)) },
                    confirmButton = {
                        TextButton(
                            onClick = {
                                showExitDialog = false
                                finish()
                            }
                        ) {
                            Text(text = getString(R.string.exit_confirm), color = MaterialTheme.colorScheme.primary)
                        }
                    },
                    dismissButton = {
                        TextButton(onClick = { showExitDialog = false }) {
                            Text(text = getString(R.string.exit_cancel), color = Color.Gray)
                        }
                    }
                )
            }
        }
    }

    /**
     * Show BiometricPrompt 모달 창을 띄웁니다.
     */
    fun showBiometricPrompt() {
        val executor = ContextCompat.getMainExecutor(this)
        val biometricPrompt = BiometricPrompt(this, executor,
            object : BiometricPrompt.AuthenticationCallback() {
                override fun onAuthenticationError(errorCode: Int, errString: CharSequence) {
                    super.onAuthenticationError(errorCode, errString)
                    sendBiometricResultToWeb(false)
                }

                override fun onAuthenticationSucceeded(result: BiometricPrompt.AuthenticationResult) {
                    super.onAuthenticationSucceeded(result)
                    sendBiometricResultToWeb(true)
                }

                override fun onAuthenticationFailed() {
                    super.onAuthenticationFailed()
                    sendBiometricResultToWeb(false)
                }
            })

        val promptInfo = BiometricPrompt.PromptInfo.Builder()
            .setTitle(getString(R.string.biometric_title))
            .setSubtitle(getString(R.string.biometric_subtitle))
            .setNegativeButtonText(getString(R.string.biometric_cancel))
            .build()

        biometricPrompt.authenticate(promptInfo)
    }

    private fun sendBiometricResultToWeb(success: Boolean) {
        runOnUiThread {
            webView?.evaluateJavascript(
                "javascript:if(window.onBiometricResult) { window.onBiometricResult($success); }", 
                null
            )
        }
    }

    override fun onNewIntent(intent: Intent?) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleDeepLinkIntent(intent)
    }

    private fun handleDeepLinkIntent(intent: Intent?) {
        intent?.getStringExtra("target_tab")?.let { tab ->
            val quotedTab = JSONObject.quote(tab)
            webView?.evaluateJavascript(
                "javascript:if(window.routeToTab) { window.routeToTab($quotedTab); }",
                null
            )
        }
    }

    override fun onDestroy() {
        webView?.destroy()
        webView = null
        super.onDestroy()
    }
}

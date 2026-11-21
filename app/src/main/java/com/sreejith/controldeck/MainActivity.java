package com.sreejith.controldeck;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.os.Build;
import android.os.Bundle;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.view.inputmethod.EditorInfo;
import android.view.inputmethod.InputMethodManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;

/**
 * Control Deck: shows the deck page served by server.py on the laptop, full screen, like an app.
 * The laptop address is asked for once and remembered.
 */
public class MainActivity extends Activity {

    private static final String PREFS = "deck";
    private static final String KEY_URL = "url";

    private WebView web;
    private LinearLayout setup;
    private EditText input;
    private TextView message;
    private String url = "";
    private volatile boolean captureVolume = true;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        if (Build.VERSION.SDK_INT >= 28) {
            WindowManager.LayoutParams lp = getWindow().getAttributes();
            lp.layoutInDisplayCutoutMode = WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES;
            getWindow().setAttributes(lp);
        }

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.BLACK);
        web = buildWebView();
        setup = buildSetup();
        root.addView(web, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        root.addView(setup, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(root);
        hideSystemUi();

        url = getSharedPreferences(PREFS, MODE_PRIVATE).getString(KEY_URL, "");
        if (url.length() == 0) {
            showSetup(null);
        } else {
            load(url);
        }
    }

    @SuppressLint({"SetJavaScriptEnabled", "AddJavascriptInterface"})
    private WebView buildWebView() {
        WebView w = new WebView(this);
        WebSettings s = w.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setCacheMode(WebSettings.LOAD_NO_CACHE);
        s.setSupportZoom(false);
        s.setBuiltInZoomControls(false);
        s.setUserAgentString(s.getUserAgentString() + " DeckApp/1");
        w.setBackgroundColor(Color.BLACK);
        w.setOverScrollMode(View.OVER_SCROLL_NEVER);
        w.setHapticFeedbackEnabled(false);
        w.setOnLongClickListener(new View.OnLongClickListener() {
            @Override
            public boolean onLongClick(View v) {
                return true;
            }
        });
        w.addJavascriptInterface(new Bridge(), "DeckApp");
        w.setWebChromeClient(new WebChromeClient());
        w.setWebViewClient(new WebViewClient() {
            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) {
                    showSetup("Cannot reach " + url + "\nIs server.py running, and are the phone and laptop on the same Wi-Fi?");
                }
            }
        });
        return w;
    }

    private int dp(int v) {
        return (int) (v * getResources().getDisplayMetrics().density + 0.5f);
    }

    private LinearLayout buildSetup() {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setGravity(Gravity.CENTER);
        box.setBackgroundColor(Color.BLACK);
        box.setPadding(dp(32), dp(24), dp(32), dp(24));

        TextView title = new TextView(this);
        title.setText("Control Deck");
        title.setTextColor(Color.WHITE);
        title.setTextSize(26);
        title.setGravity(Gravity.CENTER);
        box.addView(title);

        message = new TextView(this);
        message.setTextColor(Color.parseColor("#999999"));
        message.setTextSize(15);
        message.setGravity(Gravity.CENTER);
        message.setPadding(0, dp(12), 0, dp(20));
        box.addView(message);

        input = new EditText(this);
        input.setHint("192.168.29.168:5000");
        input.setHintTextColor(Color.parseColor("#666666"));
        input.setTextColor(Color.WHITE);
        input.setSingleLine(true);
        input.setImeOptions(EditorInfo.IME_ACTION_GO);
        input.setOnEditorActionListener(new TextView.OnEditorActionListener() {
            @Override
            public boolean onEditorAction(TextView v, int actionId, KeyEvent event) {
                connect();
                return true;
            }
        });
        box.addView(input, new LinearLayout.LayoutParams(dp(300), ViewGroup.LayoutParams.WRAP_CONTENT));

        Button go = new Button(this);
        go.setText("Connect");
        go.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                connect();
            }
        });
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(dp(300), ViewGroup.LayoutParams.WRAP_CONTENT);
        lp.topMargin = dp(16);
        box.addView(go, lp);
        return box;
    }

    private void showSetup(String error) {
        web.setVisibility(View.GONE);
        setup.setVisibility(View.VISIBLE);
        message.setText(error != null ? error
                : "Type the address that server.py prints on the laptop, for example 192.168.29.168:5000");
        input.setText(url.replace("http://", ""));
        input.setSelection(input.getText().length());
    }

    private void connect() {
        String t = input.getText().toString().trim();
        if (t.length() == 0) {
            message.setText("Type the laptop address first.");
            return;
        }
        if (t.startsWith("http://")) {
            t = t.substring(7);
        } else if (t.startsWith("https://")) {
            t = t.substring(8);
        }
        while (t.endsWith("/")) {
            t = t.substring(0, t.length() - 1);
        }
        if (t.indexOf(':') < 0) {
            t = t + ":5000";
        }
        url = "http://" + t;
        getSharedPreferences(PREFS, MODE_PRIVATE).edit().putString(KEY_URL, url).apply();
        InputMethodManager imm = (InputMethodManager) getSystemService(INPUT_METHOD_SERVICE);
        if (imm != null) {
            imm.hideSoftInputFromWindow(input.getWindowToken(), 0);
        }
        load(url);
    }

    private void load(String address) {
        setup.setVisibility(View.GONE);
        web.setVisibility(View.VISIBLE);
        web.loadUrl(address);
        hideSystemUi();
    }

    @SuppressWarnings("deprecation")
    private void hideSystemUi() {
        if (Build.VERSION.SDK_INT >= 30) {
            WindowInsetsController c = getWindow().getInsetsController();
            if (c != null) {
                c.hide(WindowInsets.Type.systemBars());
                c.setSystemBarsBehavior(WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
            }
        } else {
            getWindow().getDecorView().setSystemUiVisibility(
                    View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                            | View.SYSTEM_UI_FLAG_FULLSCREEN
                            | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION);
        }
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) {
            hideSystemUi();
        }
    }

    private boolean isVolumeKey(int keyCode) {
        return keyCode == KeyEvent.KEYCODE_VOLUME_UP || keyCode == KeyEvent.KEYCODE_VOLUME_DOWN;
    }

    /** The phone's volume buttons turn the laptop volume up and down while the deck is showing. */
    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if (captureVolume && isVolumeKey(keyCode) && web.getVisibility() == View.VISIBLE) {
            int dir = keyCode == KeyEvent.KEYCODE_VOLUME_UP ? 1 : -1;
            web.evaluateJavascript("window.deckVol && window.deckVol(" + dir + ")", null);
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    @Override
    public boolean onKeyUp(int keyCode, KeyEvent event) {
        if (captureVolume && isVolumeKey(keyCode) && web.getVisibility() == View.VISIBLE) {
            return true;
        }
        return super.onKeyUp(keyCode, event);
    }

    @Override
    @SuppressWarnings("deprecation")
    public void onBackPressed() {
        // Stay in the deck. Use the home button to leave.
    }

    /** What the deck page can ask of the app. */
    private class Bridge {
        @JavascriptInterface
        public void changeAddress() {
            runOnUiThread(new Runnable() {
                @Override
                public void run() {
                    showSetup(null);
                }
            });
        }

        @JavascriptInterface
        public void volumeKeys(boolean on) {
            captureVolume = on;
        }

        @JavascriptInterface
        public String address() {
            return url;
        }
    }
}

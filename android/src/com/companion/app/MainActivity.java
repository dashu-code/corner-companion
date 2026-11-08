package com.companion.app;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.text.InputType;
import android.view.ViewGroup;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.FrameLayout;

/**
 * 壳：用 WebView 加载 PC 上的网页界面。
 *
 * 为什么用 WebView 而不是原生界面：
 *   界面只写一遍（网页），App 只补两件网页做不到的事——常驻 + 系统通知。
 */
public class MainActivity extends Activity {

    public static final String PREFS = "companion";
    public static final String KEY_SERVER = "server_url";   // 例如 http://192.168.1.100:8787
    public static final String KEY_TOKEN = "token";         // 访问口令
    public static final String KEY_LAST = "last_notified_id";

    private WebView web;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        SharedPreferences sp = getSharedPreferences(PREFS, MODE_PRIVATE);
        String server = sp.getString(KEY_SERVER, "");

        if (server == null || server.isEmpty()) {
            askServer(sp);
            return;
        }
        mountWeb(sp);

        if (Build.VERSION.SDK_INT >= 33) {
            requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"}, 1);
        }

        checkUpdate(sp);

        Intent svc = new Intent(this, PollService.class);
        if (Build.VERSION.SDK_INT >= 26) startForegroundService(svc);
        else startService(svc);
        // 看门狗：服务被杀掉后最多 15 分钟能被拉回来
        PollService.scheduleWatchdog(this);
    }

    /** 输入框里粘完整网址（含 ?t=口令），这里拆成 base + token 存起来。 */
    private void askServer(final SharedPreferences sp) {
        final EditText input = new EditText(this);
        input.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        input.setHint("https://YOUR_SERVER_HOST:8443/?t=口令");
        input.setText("https://");

        new AlertDialog.Builder(this)
                .setTitle("服务端地址")
                .setMessage("填电脑上启动时打印的完整网址（含 ?t= 口令）。")
                .setView(input)
                .setCancelable(false)
                .setPositiveButton("确定", (d, w) -> {
                    String raw = input.getText().toString().trim();
                    if (!raw.startsWith("http")) raw = "http://" + raw;

                    String token = Uri.parse(raw).getQueryParameter("t");
                    String base = raw;
                    int q = base.indexOf('?');
                    if (q >= 0) base = base.substring(0, q);
                    if (base.endsWith("/")) base = base.substring(0, base.length() - 1);

                    sp.edit()
                            .putString(KEY_SERVER, base)
                            .putString(KEY_TOKEN, token == null ? "" : token)
                            .apply();
                    recreate();
                })
                .show();
    }

    private void mountWeb(SharedPreferences sp) {
        String base = sp.getString(KEY_SERVER, "");
        String token = sp.getString(KEY_TOKEN, "");
        String url = base + "/" + (token == null || token.isEmpty() ? "" : "?t=" + token);

        FrameLayout root = new FrameLayout(this);
        web = new WebView(this);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setSupportZoom(false);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        // 让网页能问一件事：通知权限开了没？没开就显示一个提示条
        web.addJavascriptInterface(new NativeBridge(), "Native");

        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onReceivedSslError(WebView view, android.webkit.SslErrorHandler handler,
                                           android.net.http.SslError error) {
                // 自签证书：只认写在 App 里的那个指纹，其它一律拒绝
                try {
                    android.net.http.SslCertificate sc = error == null ? null : error.getCertificate();
                    if (sc != null) {
                        android.os.Bundle b = android.net.http.SslCertificate.saveState(sc);
                        byte[] der = (b == null) ? null : b.getByteArray("x509-certificate");
                        java.security.cert.X509Certificate cert = TlsPin.fromDer(der);
                        if (cert != null && TlsPin.matches(cert)) {
                            handler.proceed();
                            return;
                        }
                    }
                } catch (Throwable ignored) { }
                handler.cancel();
                showOfflineDialog(sp, "服务器证书和 App 里锁定的不一致，已拒绝连接。\n（换过证书就要重装 App）");
            }

            @Override
            public void onReceivedError(WebView view, int errorCode, String description, String failingUrl) {
                showOfflineDialog(sp, description);
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, String u) {
                // 同一台服务器（不管 http/https、不管端口）都留在 App 里，
                // 这样 http→https 的自动迁移不会被甩到浏览器去。
                if (u != null) {
                    try {
                        String baseHost = Uri.parse(base).getHost();
                        String targetHost = Uri.parse(u).getHost();
                        if (baseHost != null && baseHost.equalsIgnoreCase(targetHost)) {
                            return false;
                        }
                    } catch (Throwable ignored) { }
                }
                // 其它外部链接才交给浏览器
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(u)));
                } catch (Throwable ignored) { }
                return true;
            }
        });
        web.loadUrl(url);
        root.addView(web, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(root);
    }

    /**
     * 检查更新：原生部分（通知/后台/配置）改了才需要新包。
     * 界面更新不用重装——那是服务器上的网页，重开就是最新的。
     * 发现新版就提示一次；点"更新"用浏览器下载，系统会弹出安装器。
     */

    /** 检查更新/下载用：https 时也走指纹锁定，避免因为自签证书连不上。 */
    private static java.net.HttpURLConnection pinnedConnection(String urlStr, int ms) throws Exception {
        java.net.HttpURLConnection c =
                (java.net.HttpURLConnection) new java.net.URL(urlStr).openConnection();
        if (c instanceof javax.net.ssl.HttpsURLConnection) {
            javax.net.ssl.HttpsURLConnection h = (javax.net.ssl.HttpsURLConnection) c;
            h.setSSLSocketFactory(TlsPin.socketFactory());
            h.setHostnameVerifier(TlsPin.ANY_HOST);
        }
        c.setConnectTimeout(ms);
        c.setReadTimeout(ms);
        return c;
    }

    private void checkUpdate(SharedPreferences sp) {
        final String base = sp.getString(KEY_SERVER, "");
        if (base == null || base.isEmpty()) return;
        if (getSharedPreferences(PREFS, MODE_PRIVATE).getBoolean("update_checking", false)) return;
        sp.edit().putBoolean("update_checking", true).apply();

        new Thread(() -> {
            try {
                String url = base + "/static/version.json";
                java.net.HttpURLConnection c = (java.net.HttpURLConnection) new java.net.URL(url).openConnection();
                c.setConnectTimeout(5000);
                c.setReadTimeout(5000);
                java.io.BufferedReader br = new java.io.BufferedReader(
                        new java.io.InputStreamReader(c.getInputStream(), "UTF-8"));
                StringBuilder sb = new StringBuilder();
                String line;
                while ((line = br.readLine()) != null) sb.append(line);
                br.close();
                c.disconnect();

                org.json.JSONObject o = new org.json.JSONObject(sb.toString());
                final long remoteCode = o.optLong("versionCode", 0);
                final String notes = o.optString("notes", "");
                final String apkPath = o.optString("apk", "");

                long curCode;
                try {
                    if (Build.VERSION.SDK_INT >= 28) {
                        curCode = getPackageManager().getPackageInfo(getPackageName(), 0).getLongVersionCode();
                    } else {
                        curCode = getPackageManager().getPackageInfo(getPackageName(), 0).versionCode;
                    }
                } catch (Throwable t) {
                    curCode = 0;
                }
                if (remoteCode <= curCode || apkPath.isEmpty()) return;

                final String dismissed = sp.getString("dismissed_version", "");
                if (dismissed.equals(String.valueOf(remoteCode))) return;

                runOnUiThread(() -> new AlertDialog.Builder(this)
                        .setTitle("有新版本")
                        .setMessage(notes.isEmpty() ? "更新后原生功能（通知/后台）才生效。界面更新不用装包。" : notes)
                        .setPositiveButton("更新", (d, w) -> {
                            try {
                                // apkPath 可能是绝对地址（用明文静态端口下载，避免浏览器弹自签证书警告）
                                  String dl = apkPath.startsWith("http") ? apkPath : (base + apkPath);
                                  downloadAndInstall(dl);
                            } catch (Throwable ignored) { }
                        })
                        .setNegativeButton("以后再说", (d, w) ->
                                sp.edit().putString("dismissed_version", String.valueOf(remoteCode)).apply())
                        .setCancelable(true)
                        .show());
            } catch (Throwable ignored) {
            } finally {
                sp.edit().putBoolean("update_checking", false).apply();
            }
        }, "update-check").start();
    }


    /** 用加密通道下载更新包，再交给系统安装器（不用浏览器）。 */
    private void downloadAndInstall(final String url) {
        final android.widget.Toast toast = android.widget.Toast.makeText(
                this, "正在下载更新…", android.widget.Toast.LENGTH_SHORT);
        toast.show();
        new Thread(() -> {
            final boolean[] okFlag = {false};
            try {
                final java.io.File out = ApkProvider.target(this);
                java.net.HttpURLConnection c = pinnedConnection(url, 60000);
                java.io.InputStream in = c.getInputStream();
                java.io.FileOutputStream fo = new java.io.FileOutputStream(out);
                byte[] buf = new byte[16384];
                int n;
                while ((n = in.read(buf)) > 0) fo.write(buf, 0, n);
                fo.flush();
                fo.close();
                in.close();
                c.disconnect();
                okFlag[0] = out.length() > 10000;   // 太小说明不是正常安装包
            } catch (Throwable ignored) { }
            runOnUiThread(() -> {
                if (!okFlag[0]) {
                    android.widget.Toast.makeText(this, "下载失败，请检查网络或稍后再试",
                            android.widget.Toast.LENGTH_LONG).show();
                    return;
                }
                try {
                    Intent i = new Intent(Intent.ACTION_VIEW);
                    i.setDataAndType(ApkProvider.URI, "application/vnd.android.package-archive");
                    i.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
                    i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                    startActivity(i);
                } catch (Throwable t) {
                    android.widget.Toast.makeText(this, "调不起安装器：" + t,
                            android.widget.Toast.LENGTH_LONG).show();
                }
            });
        }, "apk-download").start();
    }

    /** 连接失败/地址填错时，给一个改地址的入口——不然只能清数据。 */
    private void showOfflineDialog(final SharedPreferences sp, String why) {
        new AlertDialog.Builder(this)
                .setTitle("连不上服务器")
                .setMessage((why == null ? "" : why) + "\n\n检查：服务器是否在跑、端口是否放行、地址和口令对不对。")
                .setPositiveButton("重试", (d, w) -> { if (web != null) web.reload(); })
                .setNeutralButton("重新设置", (d, w) -> askServer(sp))
                .setNegativeButton("退出", (d, w) -> finish())
                .setCancelable(true)
                .show();
    }

    /**
     * 回到前台时：撤掉消息通知，并把轮询基线对齐到当前最大 id。
     * 不这样做的话，你看过的消息下次轮询还会再弹一次。
     */
    @Override
    protected void onResume() {
        super.onResume();
        try {
            NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
            if (nm != null) nm.cancel(2);
        } catch (Throwable ignored) { }

        final SharedPreferences sp = getSharedPreferences(PREFS, MODE_PRIVATE);
        final String base = sp.getString(KEY_SERVER, "");
        final String token = sp.getString(KEY_TOKEN, "");
        if (base == null || base.isEmpty()) return;

        new Thread(() -> {
            try {
                String url = base + "/api/health" + (token == null || token.isEmpty() ? "" : "?t=" + token);
                java.net.HttpURLConnection c = (java.net.HttpURLConnection)
                        new java.net.URL(url).openConnection();
                c.setConnectTimeout(5000);
                c.setReadTimeout(5000);
                java.io.BufferedReader br = new java.io.BufferedReader(
                        new java.io.InputStreamReader(c.getInputStream(), "UTF-8"));
                StringBuilder sb = new StringBuilder();
                String line;
                while ((line = br.readLine()) != null) sb.append(line);
                br.close();
                c.disconnect();
                long maxId = new org.json.JSONObject(sb.toString()).optLong("max_id", 0);
                sp.edit().putLong(KEY_LAST, maxId).apply();
            } catch (Throwable ignored) { }
        }, "baseline-sync").start();
    }

    private long lastBackAt = 0;

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) {
            web.goBack();
            return;
        }
        long now = System.currentTimeMillis();
        if (now - lastBackAt < 2000) {
            super.onBackPressed();
        } else {
            lastBackAt = now;
            android.widget.Toast.makeText(this, "再按一次退出；长按返回键可改服务器地址",
                    android.widget.Toast.LENGTH_SHORT).show();
        }
    }

    /** 长按返回键 = 重新设置服务器（WebView 里没有设置菜单的替代方案）。 */
    @Override
    public boolean onKeyLongPress(int keyCode, android.view.KeyEvent event) {
        if (keyCode == android.view.KeyEvent.KEYCODE_BACK) {
            askServer(getSharedPreferences(PREFS, MODE_PRIVATE));
            return true;
        }
        return super.onKeyLongPress(keyCode, event);
    }
    /** 网页 ↔ App 的一点点桥：只用来查/开通知权限、发测试通知。 */
    private class NativeBridge {
        @android.webkit.JavascriptInterface
        public void updateServer(final String base) {
            // 网页发现自己在 https 上跑（服务端 302 迁移过来的），
            // 就把新地址写回 App，后台轮询也跟着改用加密地址，避免每次多一跳。
            try {
                if (base == null || !base.startsWith("https://")) return;
                final SharedPreferences sp = getSharedPreferences(PREFS, MODE_PRIVATE);
                String cur = sp.getString(KEY_SERVER, "");
                if (base.equals(cur)) return;
                sp.edit().putString(KEY_SERVER, base).apply();
                Intent svc = new Intent(MainActivity.this, PollService.class);
                stopService(svc);
                if (Build.VERSION.SDK_INT >= 26) startForegroundService(svc);
                else startService(svc);
            } catch (Throwable ignored) { }
        }

        @android.webkit.JavascriptInterface
        public String appVersion() {
            try {
                android.content.pm.PackageInfo pi =
                        getPackageManager().getPackageInfo(getPackageName(), 0);
                long vc = (Build.VERSION.SDK_INT >= 28)
                        ? pi.getLongVersionCode() : pi.versionCode;
                return pi.versionName + " (" + vc + ")";
            } catch (Throwable t) {
                return "未知";
            }
        }

        @android.webkit.JavascriptInterface
        public boolean canNotify() {
            try {
                if (Build.VERSION.SDK_INT < 24) return true;
                NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
                return nm != null && nm.areNotificationsEnabled();
            } catch (Throwable t) {
                return false;
            }
        }

        @android.webkit.JavascriptInterface
        public void openNotificationSettings() {
            try {
                Intent it;
                if (Build.VERSION.SDK_INT >= 26) {
                    it = new Intent(android.provider.Settings.ACTION_APP_NOTIFICATION_SETTINGS);
                    it.putExtra(android.provider.Settings.EXTRA_APP_PACKAGE, getPackageName());
                } else {
                    it = new Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS);
                    it.setData(Uri.parse("package:" + getPackageName()));
                }
                startActivity(it);
            } catch (Throwable ignored) { }
        }

        @android.webkit.JavascriptInterface
        public String notifStatus() {
            try {
                NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
                if (nm == null) return "{\"enabled\":false,\"importance\":-1}";
                boolean en = nm.areNotificationsEnabled();
                int importance = -1;
                if (Build.VERSION.SDK_INT >= 26) {
                    NotificationChannel ch = nm.getNotificationChannel(PollService.CH_MSG);
                    if (ch != null) importance = ch.getImportance();
                }
                return "{\"enabled\":" + en + ",\"importance\":" + importance + "}";
            } catch (Throwable t) {
                return "{\"enabled\":false,\"importance\":-1}";
            }
        }

        @android.webkit.JavascriptInterface
        public String notifDiag() {
            try {
                NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
                boolean en = nm != null && nm.areNotificationsEnabled();
                int importance = -1;
                String chName = "";
                if (Build.VERSION.SDK_INT >= 26 && nm != null) {
                    NotificationChannel ch = nm.getNotificationChannel(PollService.CH_MSG);
                    if (ch != null) {
                        importance = ch.getImportance();
                        chName = String.valueOf(ch.getName());
                    }
                }
                String err = PollService.postTest(MainActivity.this);
                int active = 0;
                try {
                    if (Build.VERSION.SDK_INT >= 23 && nm != null) {
                        active = nm.getActiveNotifications().length;
                    }
                } catch (Throwable ignored) { }
                Thread.sleep(300);
                int active2 = active;
                try {
                    if (Build.VERSION.SDK_INT >= 23 && nm != null) {
                        active2 = nm.getActiveNotifications().length;
                    }
                } catch (Throwable ignored) { }
                return "{\"enabled\":" + en + ",\"importance\":" + importance
                        + ",\"channel\":\"" + chName + "\",\"posted\":" + (err == null)
                        + ",\"error\":\"" + (err == null ? "" : err.replace("\"", "'")) + "\","
                        + "\"active\":" + active2 + "}";
            } catch (Throwable t) {
                return "{\"enabled\":false,\"importance\":-1,\"error\":\"" + t.getClass().getSimpleName() + "\"}";
            }
        }

        @android.webkit.JavascriptInterface
        public void testNotify() {
            try {
                PollService.postTest(MainActivity.this);
            } catch (Throwable ignored) { }
        }
    }
}

package com.companion.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Build;
import android.os.IBinder;
import android.util.Log;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * 常驻轮询 + 系统通知。
 *
 * 为什么不用推送服务：这台手机没有 GMS，FCM 走不通；而"它主动找你"只需要
 * 十几分钟的延迟，轮询完全够（异步设计本来就不要求秒回）。
 *
 * 为什么是前台服务：普通后台线程会被 ColorOS 杀掉；前台服务有常驻通知，
 * 系统不易回收，代价是通知栏常驻一条。
 */
public class PollService extends Service {

    private static final String TAG = "CompanionPoll";
    private static final String CH_ONGOING = "companion_ongoing";
    public static final String CH_MSG = "companion_msg2";  // 换 id：旧渠道若被静音，新渠道默认开启
    private static final int NOTI_ONGOING = 1;
    private static final int NOTI_MSG = 2;

    /** 轮询间隔：5 分钟。想更省电就调大；想更快收到就调小。 */
    private static final long WAIT_SECONDS = 50L;   // 长轮询挂 50 秒
    private static final long ERROR_RETRY_MS = 5 * 1000L;

    private volatile boolean running = false;
    private Thread worker;

    @Override
    public void onCreate() {
        super.onCreate();
        createChannels();
        startForeground(NOTI_ONGOING, buildOngoing());
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (!running) {
            running = true;
            worker = new Thread(this::loop, "poll-loop");
            worker.setDaemon(true);
            worker.start();
        }
        return START_STICKY;
    }

    private void loop() {
        while (running) {
            try {
                pollOnce();
            } catch (Throwable t) {
                Log.w(TAG, "poll failed: " + t);
                try {
                    Thread.sleep(ERROR_RETRY_MS);
                } catch (InterruptedException e) {
                    return;
                }
            }
        }
    }

    /** 口令改走请求头：URL 里不带口令，日志/代理都记不到。 */
    private volatile String authToken = "";

    private String withToken(String base, String token, String pathAndQuery) {
        authToken = (token == null) ? "" : token;
        return base + pathAndQuery;
    }


    /** 把 App 端的关键步骤发到服务器日志（排查通知问题用）。 */
    private void report(String msg) {
        try {
            SharedPreferences sp = getSharedPreferences(MainActivity.PREFS, MODE_PRIVATE);
            String base = sp.getString(MainActivity.KEY_SERVER, "");
            String token = sp.getString(MainActivity.KEY_TOKEN, "");
            if (base == null || base.isEmpty()) return;
            authToken = (token == null) ? "" : token;
            String url = base + "/api/applog?m=" + java.net.URLEncoder.encode(msg, "UTF-8");
            HttpURLConnection c = open(url, 5000, 5000);
            c.getInputStream().close();
            c.disconnect();
        } catch (Throwable ignored) { }
    }

    private void pollOnce() throws Exception {
        SharedPreferences sp = getSharedPreferences(MainActivity.PREFS, MODE_PRIVATE);
        String base = sp.getString(MainActivity.KEY_SERVER, "");
        String token = sp.getString(MainActivity.KEY_TOKEN, "");
        if (base == null || base.isEmpty()) return;

        long last = sp.getLong(MainActivity.KEY_LAST, -1);

        if (last < 0) {
            // 第一次：只对齐基线，不打扰
            JSONObject health = getJson(withToken(base, token, "/api/health"));
            sp.edit().putLong(MainActivity.KEY_LAST, health.optLong("max_id", 0)).apply();
            report("服务启动，基线 max_id=" + health.optLong("max_id", 0));
            return;
        }

        // 长轮询：服务器挂着请求，一有新消息就立刻返回 -> 通知几乎即时（接近微信）
        JSONObject r = getJson(
                withToken(base, token, "/api/wait?after=" + last + "&timeout=" + WAIT_SECONDS),
                (int) (WAIT_SECONDS + 15) * 1000);
        int n = r.optInt("new", 0);
        long maxId = r.optLong("max_id", last);
        // 先保存进度再弹通知：万一通知那步抛异常，也不会下一轮把同一条再弹一次
        if (maxId > last) sp.edit().putLong(MainActivity.KEY_LAST, maxId).apply();
        if (n > 0) {
            String preview = r.optString("preview", "有新消息");
            report("收到新消息 n=" + n + " id=" + maxId);
            try {
                notifyMessage(preview);
                report("notify 已调用 preview=" + preview);
            } catch (Throwable t) {
                report("notify 失败: " + t.getClass().getSimpleName() + ": " + t.getMessage());
            }
        }
    }


    /** 打开连接：https 时套上用指纹锁定的 socket 工厂（自签证书也能连）。 */
    private HttpURLConnection open(String urlStr, int connectMs, int readMs) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(urlStr).openConnection();
        if (c instanceof javax.net.ssl.HttpsURLConnection) {
            javax.net.ssl.HttpsURLConnection h = (javax.net.ssl.HttpsURLConnection) c;
            h.setSSLSocketFactory(TlsPin.socketFactory());
            h.setHostnameVerifier(TlsPin.ANY_HOST);
        }
        String tk = authToken;
        if (tk != null && !tk.isEmpty()) {
            c.setRequestProperty("X-Companion-Token", tk);
        }
        c.setConnectTimeout(connectMs);
        c.setReadTimeout(readMs);
        return c;
    }

    private JSONObject getJson(String urlStr) throws Exception {
        return getJson(urlStr, 8000);
    }

    private JSONObject getJson(String urlStr, int readTimeoutMs) throws Exception {
        HttpURLConnection c = open(urlStr, 8000, readTimeoutMs);
        c.setRequestMethod("GET");
        try {
            try (BufferedReader br = new BufferedReader(new InputStreamReader(c.getInputStream(), "UTF-8"))) {
                StringBuilder sb = new StringBuilder();
                String line;
                while ((line = br.readLine()) != null) sb.append(line);
                return new JSONObject(sb.toString());
            }
        } finally {
            // 自愈：被 302 到 https 说明服务端要求加密，把新地址记下来，
            // 以后直接用加密地址，不再依赖网页有没有调用 updateServer。
            try {
                String finalUrl = String.valueOf(c.getURL());
                if (urlStr.startsWith("http://") && finalUrl.startsWith("https://")) {
                    int slash = finalUrl.indexOf('/', "https://".length());
                    String newBase = slash > 0 ? finalUrl.substring(0, slash) : finalUrl;
                    SharedPreferences sp = getSharedPreferences(MainActivity.PREFS, MODE_PRIVATE);
                    if (!newBase.equals(sp.getString(MainActivity.KEY_SERVER, ""))) {
                        sp.edit().putString(MainActivity.KEY_SERVER, newBase).apply();
                        report("地址自愈 -> " + newBase);
                    }
                }
            } catch (Throwable ignored) { }
            c.disconnect();
        }
    }


    /** 发一条测试通知：返回 null 表示发出去了，否则返回错误原因。 */
    public static String postTest(Context ctx) {
        try {
            NotificationManager nm = (NotificationManager) ctx.getSystemService(NOTIFICATION_SERVICE);
            if (Build.VERSION.SDK_INT >= 26) {
                NotificationChannel ch = new NotificationChannel(CH_MSG, "来消息", NotificationManager.IMPORTANCE_HIGH);
                ch.enableVibration(true);
                nm.createNotificationChannel(ch);
            }
            Notification.Builder b = (Build.VERSION.SDK_INT >= 26)
                    ? new Notification.Builder(ctx, CH_MSG)
                    : new Notification.Builder(ctx);
            b.setContentTitle("角落")
                    .setContentText("这是一条测试通知，能看到说明通道是通的")
                    .setStyle(new Notification.BigTextStyle().bigText("这是一条测试通知，能看到说明通道是通的"))
                    .setSmallIcon(android.R.drawable.ic_dialog_email)
                    .setAutoCancel(true);
            nm.notify(NOTI_MSG, b.build());
            return null;
        } catch (Throwable t) {
            return t.getClass().getSimpleName() + ": " + t.getMessage();
        }
    }


    /** 被从最近任务划掉时：2 秒后拉一次，并挂上 15 分钟一次的看门狗。 */
    @Override
    public void onTaskRemoved(Intent rootIntent) {
        scheduleRevive(2000);
        scheduleWatchdog(this);
        super.onTaskRemoved(rootIntent);
    }

    private void scheduleRevive(long delayMs) {
        try {
            android.app.AlarmManager am = (android.app.AlarmManager) getSystemService(ALARM_SERVICE);
            Intent i = new Intent(this, BootReceiver.class);
            i.setAction("com.companion.app.REVIVE");
            PendingIntent pi = PendingIntent.getBroadcast(this, 0, i,
                    PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
            if (Build.VERSION.SDK_INT >= 23) {
                am.setAndAllowWhileIdle(android.app.AlarmManager.RTC_WAKEUP,
                        System.currentTimeMillis() + delayMs, pi);
            } else {
                am.set(android.app.AlarmManager.RTC_WAKEUP, System.currentTimeMillis() + delayMs, pi);
            }
        } catch (Throwable ignored) { }
    }

    /** 看门狗：每 15 分钟叫醒一次，服务死了就重新起来（系统允许的前提下）。 */
    public static void scheduleWatchdog(Context ctx) {
        try {
            android.app.AlarmManager am = (android.app.AlarmManager) ctx.getSystemService(Context.ALARM_SERVICE);
            Intent i = new Intent(ctx, BootReceiver.class);
            i.setAction("com.companion.app.WATCHDOG");
            PendingIntent pi = PendingIntent.getBroadcast(ctx, 1, i,
                    PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
            long every = 15 * 60 * 1000L;
            am.setInexactRepeating(android.app.AlarmManager.RTC_WAKEUP,
                    System.currentTimeMillis() + every, every, pi);
        } catch (Throwable ignored) { }
    }

    private void createChannels() {
        if (Build.VERSION.SDK_INT < 26) return;
        NotificationManager nm = getSystemService(NotificationManager.class);

        NotificationChannel ongoing = new NotificationChannel(
                CH_ONGOING, "常驻运行", NotificationManager.IMPORTANCE_MIN);
        ongoing.setShowBadge(false);
        nm.createNotificationChannel(ongoing);

        NotificationChannel msg = new NotificationChannel(
                CH_MSG, "来消息", NotificationManager.IMPORTANCE_HIGH);
        msg.setShowBadge(true);
        nm.createNotificationChannel(msg);
    }

    @SuppressWarnings("deprecation")
    private Notification buildOngoing() {
        Notification.Builder b = (Build.VERSION.SDK_INT >= 26)
                ? new Notification.Builder(this, CH_ONGOING)
                : new Notification.Builder(this);
        return b.setContentTitle("角落")
                .setContentText("在等你")
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .setOngoing(true)
                .setContentIntent(openApp())
                .build();
    }

    @SuppressWarnings("deprecation")
    private void notifyMessage(String preview) {
        Notification.Builder b = (Build.VERSION.SDK_INT >= 26)
                ? new Notification.Builder(this, CH_MSG)
                : new Notification.Builder(this);
        b.setContentTitle("它")
                .setContentText(preview)
                .setStyle(new Notification.BigTextStyle().bigText(preview))
                .setSmallIcon(android.R.drawable.ic_dialog_email)
                .setAutoCancel(true)
                .setContentIntent(openApp());
        if (Build.VERSION.SDK_INT >= 21) b.setCategory(Notification.CATEGORY_MESSAGE);
        getSystemService(NotificationManager.class).notify(NOTI_MSG, b.build());
    }

    private PendingIntent openApp() {
        Intent i = new Intent(this, MainActivity.class);
        i.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= 23) flags |= PendingIntent.FLAG_IMMUTABLE;
        return PendingIntent.getActivity(this, 0, i, flags);
    }

    @Override
    public IBinder onBind(Intent intent) { return null; }

    @Override
    public void onDestroy() {
        running = false;
        if (worker != null) worker.interrupt();
        super.onDestroy();
    }
}

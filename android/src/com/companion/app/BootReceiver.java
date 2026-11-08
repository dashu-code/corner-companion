package com.companion.app;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

/**
 * 保活用：开机自启 + 被划掉后的复活闹钟 + 看门狗。
 * 系统广播（开机）和 App 自己发的闹钟都走这里，统一拉起前台服务。
 */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context ctx, Intent intent) {
        try {
            Intent svc = new Intent(ctx, PollService.class);
            if (Build.VERSION.SDK_INT >= 26) ctx.startForegroundService(svc);
            else ctx.startService(svc);
        } catch (Throwable ignored) {
        }
    }
}

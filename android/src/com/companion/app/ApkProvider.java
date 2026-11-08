package com.companion.app;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;

import java.io.File;
import java.io.FileNotFoundException;

/**
 * 只用来把"下载好的更新包"交给系统安装器。
 *
 * 为什么需要它：targetSdk >= 24 之后不允许用 file:// 分享文件，
 * 必须走 content:// + 临时读权限。这里只需要 openFile 一个方法。
 */
public class ApkProvider extends ContentProvider {

    public static final String AUTHORITY = "com.companion.app.apk";
    public static final Uri URI = Uri.parse("content://" + AUTHORITY + "/update.apk");

    private static File apkFile(android.content.Context ctx) {
        return new File(ctx.getCacheDir(), "update.apk");
    }

    /** 下载完成后的落点。 */
    public static File target(android.content.Context ctx) {
        return apkFile(ctx);
    }

    @Override
    public boolean onCreate() {
        return true;
    }

    @Override
    public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        File f = apkFile(getContext());
        if (!f.isFile()) throw new FileNotFoundException("还没有下载好的安装包");
        return ParcelFileDescriptor.open(f, ParcelFileDescriptor.MODE_READ_ONLY);
    }

    @Override
    public String getType(Uri uri) {
        return "application/vnd.android.package-archive";
    }

    @Override
    public Cursor query(Uri uri, String[] projection, String selection,
                        String[] selectionArgs, String sortOrder) {
        return null;
    }

    @Override
    public Uri insert(Uri uri, ContentValues values) {
        return null;
    }

    @Override
    public int delete(Uri uri, String selection, String[] selectionArgs) {
        return 0;
    }

    @Override
    public int update(Uri uri, ContentValues values, String selection, String[] selectionArgs) {
        return 0;
    }
}

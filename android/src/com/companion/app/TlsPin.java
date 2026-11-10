package com.companion.app;

import java.io.ByteArrayInputStream;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.security.cert.Certificate;
import java.security.cert.CertificateFactory;
import java.security.cert.X509Certificate;

import javax.net.ssl.HostnameVerifier;
import javax.net.ssl.SSLContext;
import javax.net.ssl.SSLSession;
import javax.net.ssl.SSLSocketFactory;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;

/**
 * 自签证书 + 指纹锁定。
 *
 * 为什么这么做：服务器没有域名（国内用域名要备案），用不了正规 CA 证书。
 * 自签证书能加密，但默认没人信任它；这里把服务器证书的 SHA-256 指纹写死在
 * App 里，只认这一个证书 —— 加密和"防中间人"同时成立：
 * 别人拿不到私钥，就签不出这个指纹的证书，代理/劫持都会被拒绝。
 *
 * 替换证书时，必须同时改这里的 SHA256 并重新打包。
 */
public final class TlsPin {

    /** 服务器证书 SHA-256 指纹（小写十六进制，不含冒号）。 */
    public static final String SHA256 =
            "9ae00afd65cbba234d57bbd05fa237d0ff50d30cba7c39ad1ec08ef4e1322701";

    private TlsPin() {}

    /** 证书编码后的 SHA-256 是否等于锁定值。 */
    public static boolean matches(Certificate cert) {
        if (cert == null) return false;
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            byte[] digest = md.digest(cert.getEncoded());
            StringBuilder sb = new StringBuilder(digest.length * 2);
            for (byte b : digest) {
                sb.append(Character.forDigit((b >> 4) & 0xF, 16));
                sb.append(Character.forDigit(b & 0xF, 16));
            }
            return SHA256.equalsIgnoreCase(sb.toString());
        } catch (Throwable t) {
            return false;
        }
    }

    /** 从 DER 字节还原证书（WebView 的 SslCertificate 就给这个）。 */
    public static X509Certificate fromDer(byte[] der) {
        if (der == null || der.length == 0) return null;
        try {
            CertificateFactory cf = CertificateFactory.getInstance("X.509");
            return (X509Certificate) cf.generateCertificate(new ByteArrayInputStream(der));
        } catch (Throwable t) {
            return null;
        }
    }

    private static final X509TrustManager PINNED = new X509TrustManager() {
        @Override
        public void checkClientTrusted(X509Certificate[] chain, String authType) {
            // 客户端证书不用
        }

        @Override
        public void checkServerTrusted(X509Certificate[] chain, String authType)
                throws java.security.cert.CertificateException {
            if (chain == null || chain.length == 0) {
                throw new java.security.cert.CertificateException("空证书链");
            }
            // 只看叶子证书的指纹；CA 链一律不认（自签本来也没有链）
            if (!matches(chain[0])) {
                throw new java.security.cert.CertificateException("证书指纹不匹配，可能被中间人替换");
            }
        }

        @Override
        public X509Certificate[] getAcceptedIssuers() {
            return new X509Certificate[0];
        }
    };

    private static SSLSocketFactory factory;

    /** 只信任锁定指纹的 SSLSocketFactory。 */
    public static synchronized SSLSocketFactory socketFactory() throws Exception {
        if (factory == null) {
            SSLContext ctx = SSLContext.getInstance("TLS");
            ctx.init(null, new TrustManager[]{PINNED}, new SecureRandom());
            factory = ctx.getSocketFactory();
        }
        return factory;
    }

    /**
     * 主机名校验直接放行：身份已经由"只认这一个指纹"保证了，
     * 再校验主机名只会因为自签证书的 CN 而误杀。
     */
    public static final HostnameVerifier ANY_HOST = new HostnameVerifier() {
        @Override
        public boolean verify(String hostname, SSLSession session) {
            return true;
        }
    };
}

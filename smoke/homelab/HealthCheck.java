// Single-file Java source launcher healthcheck (JEP 330, Java 11+).
// Usage: java HealthCheck.java <url>
// Exits 0 on HTTP 200, 1 otherwise. Used because the mock-oidc image is
// distroless (no shell, wget, or curl) so only java itself is available.
public class HealthCheck {
    public static void main(String[] args) throws Exception {
        String urlStr = args.length > 0 ? args[0]
            : "http://localhost:8080/default/.well-known/openid-configuration";
        var conn = (java.net.HttpURLConnection)
            new java.net.URL(urlStr).openConnection();
        conn.setConnectTimeout(3000);
        conn.setReadTimeout(3000);
        conn.setRequestMethod("GET");
        int code = conn.getResponseCode();
        System.exit(code == 200 ? 0 : 1);
    }
}

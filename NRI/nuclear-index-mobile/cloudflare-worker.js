// Cloudflare Worker — private CORS proxy for the Nuclear Renaissance Index site.
//
// Deploy in ~60 seconds:
//   1. Go to https://workers.cloudflare.com/ and sign in (free tier works fine).
//   2. Create -> Worker -> give it a name (anything, e.g. "nri-proxy").
//   3. Click "Edit code", delete the placeholder, paste THIS ENTIRE FILE.
//   4. Click "Save and Deploy".
//   5. Copy the *.workers.dev URL Cloudflare gives you.
//   6. In the NRI site, click "Data source", paste:
//         https://<your-worker>.workers.dev/?url=
//      ...and click Save. That's it — no more public-proxy rate limits.
//
// Security note: by default this worker accepts any URL. If you want to lock
// it down, set ALLOW_HOSTS below to a whitelist of hostnames.

const ALLOW_HOSTS = null;  // e.g. ["query1.finance.yahoo.com", "stooq.com"]

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const target = url.searchParams.get("url");
    if (!target) {
      return new Response("Usage: ?url=<encoded target URL>", {
        status: 400,
        headers: corsHeaders(),
      });
    }
    let parsed;
    try { parsed = new URL(target); }
    catch (e) { return new Response("Invalid url", { status: 400, headers: corsHeaders() }); }
    if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
      return new Response("Only http/https allowed", { status: 400, headers: corsHeaders() });
    }
    if (ALLOW_HOSTS && !ALLOW_HOSTS.includes(parsed.hostname)) {
      return new Response("Host not allowed", { status: 403, headers: corsHeaders() });
    }
    try {
      const upstream = await fetch(parsed.toString(), {
        method: request.method,
        headers: {
          "User-Agent": "Mozilla/5.0 (compatible; NRI-Proxy/1.0)",
          "Accept": "*/*",
        },
        redirect: "follow",
      });
      const body = await upstream.arrayBuffer();
      const h = corsHeaders();
      const ct = upstream.headers.get("Content-Type");
      if (ct) h["Content-Type"] = ct;
      return new Response(body, { status: upstream.status, headers: h });
    } catch (e) {
      return new Response("Upstream error: " + e.message, { status: 502, headers: corsHeaders() });
    }
  },
};

function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Cache-Control": "no-store",
  };
}

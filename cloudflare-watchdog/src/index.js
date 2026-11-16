/**
 * CRYPTO SUPER RADAR - Render Free keep-alive watchdog.
 *
 * This worker only makes a public GET request to the Render /health endpoint.
 * It never authenticates to BloFin, reads account data, or submits trade orders.
 */
const DEFAULT_HEALTH_URL =
  "https://crypto-super-radar-blofin-demo.onrender.com/health";

export default {
  async scheduled(controller, env, ctx) {
    const target = (env.RENDER_HEALTH_URL || DEFAULT_HEALTH_URL).trim();
    const startedAt = Date.now();

    try {
      const response = await fetch(target, {
        method: "GET",
        headers: {
          "Cache-Control": "no-cache, no-store",
          "Pragma": "no-cache",
          "User-Agent": "CSR-Render-Watchdog/1.0",
        },
        redirect: "follow",
      });

      const raw = await response.text();
      let payload = null;
      try {
        payload = JSON.parse(raw);
      } catch {
        // A non-JSON response is treated as a failed health check below.
      }

      const healthy = response.ok && payload?.status === "ok";
      const result = {
        event: "render_health_check",
        scheduledAt: new Date(controller.scheduledTime).toISOString(),
        checkedAt: new Date().toISOString(),
        target,
        httpStatus: response.status,
        healthStatus: payload?.status || "unknown",
        commit: payload?.commit || null,
        elapsedMs: Date.now() - startedAt,
        healthy,
      };

      console.log(JSON.stringify(result));
      if (!healthy) {
        throw new Error(
          `Render health check failed: HTTP ${response.status}, status=${payload?.status || "unknown"}`
        );
      }
    } catch (error) {
      console.error(JSON.stringify({
        event: "render_health_check_error",
        checkedAt: new Date().toISOString(),
        target,
        elapsedMs: Date.now() - startedAt,
        error: String(error?.message || error).slice(0, 300),
      }));
      throw error;
    }
  },

  // Public manual probe for the watchdog itself. It performs no outbound call.
  async fetch(request) {
    const url = new URL(request.url);
    if (url.pathname === "/health") {
      return new Response(JSON.stringify({
        status: "ok",
        worker: "csr-render-watchdog",
        purpose: "Render keep-alive only; no exchange access and no trading actions",
      }), {
        headers: {
          "Content-Type": "application/json; charset=utf-8",
          "Cache-Control": "no-store",
        },
      });
    }
    return new Response(
      "CSR Render Watchdog is deployed. Scheduled checks only call Render GET /health.",
      { headers: { "Content-Type": "text/plain; charset=utf-8" } }
    );
  },
};

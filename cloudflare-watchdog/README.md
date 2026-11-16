# CRYPTO SUPER RADAR - Cloudflare Render Watchdog

This small Cloudflare Worker is a **keep-alive and health-check helper only**. It does not connect to BloFin, read account data, manage positions, or submit orders.

## Why it exists

The BloFin Demo AutoPilot currently runs inside the Render web-service process. Render Free services can spin down after 15 minutes without inbound traffic. When the Render process starts, the application's own startup code starts the Demo AutoPilot again. This Worker sends one HTTP GET to the public health endpoint every five minutes so the Render service normally receives traffic before its idle timeout.

## Deploy

1. In Cloudflare, open **Workers & Pages** and create a Worker named `csr-render-watchdog`.
2. Deploy the code in `src/index.js` (or connect this GitHub repository and select branch `ops/cloudflare-render-watchdog`, directory `cloudflare-watchdog`).
3. Set the Cron Trigger to **`*/5 * * * *`** (every five minutes). The included `wrangler.toml` already declares this schedule when using Wrangler/Git integration.
4. Set the variable `RENDER_HEALTH_URL` to:
   `https://crypto-super-radar-blofin-demo.onrender.com/health`
5. Deploy and confirm successful entries under **Workers & Pages → csr-render-watchdog → Logs / Cron Events**.

No BloFin credentials, dashboard token, database token, or other secret belongs in this Worker. The URL is public and only exposes the application's existing health check.

## Validation

A successful run should log:
- `event: render_health_check`
- `httpStatus: 200`
- `healthStatus: ok`
- `healthy: true`

The Worker also exposes its own `/health` route for a manual deployment check. That route does not contact Render.

## Important limits

This is a **best-effort free-tier workaround**, not an uptime guarantee or an SLA. Render may restart a Free instance at any time, free services share 750 instance-hours per workspace per calendar month, and platform or network outages can still interrupt checks. A service kept awake all month uses roughly 720 hours, leaving little room for other active Free services in the same workspace. Render's own documentation recommends paid compute for removing Free-instance limitations.

For a stronger production arrangement, run the trader as a separate always-on paid Background Worker (or equivalent always-on compute), retain Turso for durable state, and keep the dashboard separate. The current Render service has **not** been upgraded or changed by adding this helper branch.

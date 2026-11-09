"""Dependency-light Persian web dashboard for on-demand research scans."""
from __future__ import annotations

import html
import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .integrated_engine import WhaleAwareConfirmationEngine
from .models import AssetSpec

ENGINE = WhaleAwareConfirmationEngine()
STORE = ENGINE.whale_store
JOBS: dict[str, dict] = {}
LAST_SCAN: dict[str, float] = {}
LOCK = threading.RLock()
SCAN_RUNNING = False


def _page(body: str, title: str = "CRYPTO SUPER RADAR") -> bytes:
    return f"""<!doctype html>
<html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
body{{font-family:Arial,sans-serif;background:#0b1220;color:#e5e7eb;margin:0;padding:24px}}
main{{max-width:980px;margin:auto}} .card{{background:#111c2f;border:1px solid #26364d;border-radius:14px;padding:18px;margin:14px 0}}
input,button{{font:inherit;padding:12px;border-radius:8px;border:1px solid #46566d;background:#0b1220;color:#fff;width:100%;box-sizing:border-box}}
button{{background:#2563eb;border:0;margin-top:12px;cursor:pointer}} table{{width:100%;border-collapse:collapse}}
td,th{{padding:9px;border-bottom:1px solid #334155;text-align:right;vertical-align:top}}
small,.muted{{color:#9ca3af}} .status{{font-weight:bold}} code{{direction:ltr;display:inline-block}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#08101d;padding:12px;border-radius:8px;direction:ltr;text-align:left}}
</style></head><body><main>{body}</main></body></html>""".encode("utf-8")


def _run_scan(job_id: str, asset: AssetSpec, headline: str, social_post: str) -> None:
    global SCAN_RUNNING
    try:
        result = ENGINE.analyze(
            asset,
            headlines=[headline] if headline else [],
            social_posts=[social_post] if social_post else [],
        )
        payload = {
            "asset": asset.raw,
            "signal": result.signal.value,
            "confirmation_score": result.confirmation_score,
            "estimated_risk_score": result.estimated_risk_score,
            "risk_score": result.metrics.get("risk_score"),
            "opportunity_rank": result.opportunity_rank,
            "confidence_level_pct": result.metrics.get("confidence_level_pct"),
            "evidence_confidence_pct": result.metrics.get("evidence_confidence_pct"),
            "summary_fa": result.summary_fa,
            "gates": [
                {"id": gate.gate_id, "name": gate.name_fa, "name_en": gate.name_en,
                 "status": gate.status.value, "reason": gate.reason_fa,
                 "reason_en": gate.reason_en, "evidence": gate.evidence}
                for gate in result.gates
            ],
            "metrics": {
                key: result.metrics.get(key) for key in (
                    "whale_status", "whale_bias", "whale_history_count",
                    "whale_recent_history_bias", "whale_alignment", "sentiment_alignment",
                    "market_sentiment_score", "fear_greed_value", "fear_greed_attribution",
                    "walk_forward_status", "walk_forward_fold_count", "risk_score",
                    "opportunity_rank", "confidence_level_pct", "evidence_confidence_pct",
                )
            },
        }
        with LOCK:
            JOBS[job_id] = {"status": "completed", "result": payload}
    except Exception as exc:
        with LOCK:
            JOBS[job_id] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        with LOCK:
            SCAN_RUNNING = False


class Handler(BaseHTTPRequestHandler):
    server_version = "CryptoSuperRadar/0.2"

    def _send(self, status: int, content: bytes, content_type: str = "text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            return self._send(200, b'{"status":"ok"}', "application/json")
        if parsed.path.startswith("/jobs/"):
            job_id = parsed.path.rsplit("/", 1)[-1]
            with LOCK:
                job = JOBS.get(job_id)
            if job is None:
                return self._send(404, b'{"error":"job not found"}', "application/json")
            return self._send(200, json.dumps(job, ensure_ascii=False, default=str).encode(),
                              "application/json; charset=utf-8")
        if parsed.path == "/history":
            asset = parse_qs(parsed.query).get("asset", [""])[0]
            try:
                AssetSpec.parse(asset)
                data = STORE.summary(asset)
                return self._send(200, json.dumps(data, ensure_ascii=False, default=str).encode(),
                                  "application/json; charset=utf-8")
            except Exception:
                return self._send(400, b'{"error":"invalid asset"}', "application/json")
        if parsed.path != "/":
            return self._send(404, b"Not found", "text/plain; charset=utf-8")
        body = """
        <h1>🐋 CRYPTO SUPER RADAR</h1>
        <p class="muted">Quantitative Crypto Intelligence · فقط تحلیل و Paper Research، بدون اجرای سفارش</p>
        <section class="card">
          <h2>اسکن دارایی</h2>
          <form method="post" action="/scan">
            <label>بازار و نماد / آدرس</label>
            <input name="asset" required value="cex:BTCUSDT" placeholder="cex:BTCUSDT یا base:0x...">
            <p class="muted">نمونه‌ها: cex:BTCUSDT · perp:BTCUSDT · base:0x... · eth:0x... · solana:MINT</p>
            <label>تیتر خبری تکمیلی (اختیاری)</label>
            <input name="headline" placeholder="تیتر خبر">
            <label>متن اجتماعی تکمیلی (اختیاری)</label>
            <input name="social_post" placeholder="متن عمومی شبکه اجتماعی">
            <button type="submit">اجرای اسکن پژوهشی</button>
          </form>
        </section>
        <section class="card">
          <h3>۱۲ فیلتر اجباری</h3>
          <p>داده بازار · رژیم · روند · مومنتوم · جریان سرمایه · امنیت · احساسات · بک‌تست · ریسک · نهنگ‌ها · پوشش احساسات · Walk-forward</p>
          <p class="muted">هر فیلتر FAIL یا UNKNOWN باشد، خروجی NO TRADE است. سولانا در نسخه فعلی فقط نمونه‌برداری محدود از تراکنش‌های اخیر دارد.</p>
        </section>
        <p class="muted">تاریخچه: <code>/history?asset=cex:BTCUSDT</code></p>
        """
        return self._send(200, _page(body))

    def do_POST(self):
        global SCAN_RUNNING
        if urlparse(self.path).path != "/scan":
            return self._send(404, b"Not found", "text/plain; charset=utf-8")
        origin = self.headers.get("Origin")
        request_host = self.headers.get("Host", "")
        if origin and urlparse(origin).netloc.lower() != request_host.lower():
            return self._send(403, b"Cross-origin scan requests are not allowed", "text/plain; charset=utf-8")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            return self._send(400, b"Invalid Content-Length", "text/plain; charset=utf-8")
        if length <= 0 or length > 16_384:
            return self._send(413, b"Invalid request size", "text/plain; charset=utf-8")
        client = self.client_address[0]
        now = time.time()
        with LOCK:
            previous = LAST_SCAN.get(client, 0.0)
            if now - previous < 60:
                return self._send(429, _page("<h2>محدودیت اسکن</h2><p>برای جلوگیری از مصرف بیش از حد APIهای عمومی، هر IP هر ۶۰ ثانیه یک اسکن می‌تواند اجرا کند.</p><a href='/'>بازگشت</a>"))
            if SCAN_RUNNING:
                return self._send(429, _page("<h2>اسکن در حال اجراست</h2><p>لطفاً تا پایان اسکن فعلی صبر کنید.</p><a href='/'>بازگشت</a>"))
            LAST_SCAN[client] = now
            SCAN_RUNNING = True
        try:
            form = parse_qs(self.rfile.read(length).decode("utf-8", errors="replace"))
            asset = AssetSpec.parse(form.get("asset", [""])[0].strip())
            headline = form.get("headline", [""])[0].strip()
            social_post = form.get("social_post", [""])[0].strip()
            job_id = uuid.uuid4().hex
            with LOCK:
                JOBS[job_id] = {"status": "queued"}
                while len(JOBS) > 100:
                    JOBS.pop(next(iter(JOBS)))
            worker = threading.Thread(
                target=_run_scan, args=(job_id, asset, headline, social_post), daemon=True
            )
            worker.start()
            body = (
                "<h2>اسکن آغاز شد</h2><p>شناسه کار: <code>" + job_id + "</code></p>"
                "<p id='state'>در حال جمع‌آوری داده‌های بازار، نهنگ‌ها و احساسات...</p>"
                "<pre id='result'>لطفاً این صفحه را باز نگه دارید.</pre><p><a href='/'>بازگشت</a></p>"
                "<script>const job='" + job_id + "';"
                "async function poll(){try{const r=await fetch('/jobs/'+job);const j=await r.json();"
                "document.getElementById('state').textContent=j.status;"
                "if(j.status==='completed'||j.status==='failed'){document.getElementById('result').textContent=JSON.stringify(j,null,2);return;}"
                "}catch(e){document.getElementById('state').textContent='ارتباط قطع شد؛ بعداً /jobs/'+job+' را بررسی کنید';return;}"
                "setTimeout(poll,2500);}poll();</script>"
            )
            return self._send(202, _page(body, "Scan Running"))
        except Exception as exc:
            with LOCK:
                SCAN_RUNNING = False
            message = html.escape(f"{type(exc).__name__}: {exc}")
            return self._send(400, _page(f"<h2>خطا در ورودی</h2><p>{message}</p><a href='/'>بازگشت</a>"))

    def log_message(self, fmt, *args):
        super().log_message(fmt, *args)


def main():
    port = int(os.getenv("PORT", "10000"))
    host = os.getenv("HOST", "0.0.0.0")
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"CRYPTO SUPER RADAR listening on {host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()

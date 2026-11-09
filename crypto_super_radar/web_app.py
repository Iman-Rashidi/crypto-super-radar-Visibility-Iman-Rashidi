"""Dependency-light Persian web dashboard for on-demand research scans."""
from __future__ import annotations

import html
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .integrated_engine import WhaleAwareConfirmationEngine
from .models import AssetSpec
from .whale_store import WhaleStore

ENGINE = WhaleAwareConfirmationEngine()
STORE = ENGINE.whale_store
LAST_SCAN: dict[str, float] = {}
LOCK = threading.Lock()


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
</style></head><body><main>{body}</main></body></html>""".encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "CryptoSuperRadar/0.1"

    def _send(self, status: int, content: bytes, content_type: str = "text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            return self._send(200, b'{"status":"ok"}', "application/json")
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
        <p class="muted">برای مشاهده تاریخچه آخرین اسکن‌ها: <code>/history?asset=cex:BTCUSDT</code></p>
        """
        return self._send(200, _page(body))

    def do_POST(self):
        if urlparse(self.path).path != "/scan":
            return self._send(404, b"Not found", "text/plain; charset=utf-8")
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 16_384:
            return self._send(413, b"Invalid request size", "text/plain; charset=utf-8")
        client = self.client_address[0]
        now = time.time()
        with LOCK:
            previous = LAST_SCAN.get(client, 0.0)
            if now - previous < 60:
                return self._send(429, _page("<h2>محدودیت اسکن</h2><p>برای جلوگیری از مصرف بیش از حد APIهای عمومی، هر IP هر ۶۰ ثانیه یک اسکن می‌تواند اجرا کند.</p><a href='/'>بازگشت</a>"))
            LAST_SCAN[client] = now
        try:
            form = parse_qs(self.rfile.read(length).decode("utf-8", errors="replace"))
            asset = AssetSpec.parse(form.get("asset", [""])[0].strip())
            headline = form.get("headline", [""])[0].strip()
            social_post = form.get("social_post", [""])[0].strip()
            result = ENGINE.analyze(
                asset, headlines=[headline] if headline else [],
                social_posts=[social_post] if social_post else [],
            )
            gates = "".join(
                "<tr><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                    html.escape(str(gate.gate_id)),
                    html.escape(str(gate.name_en)),
                    html.escape(str(gate.status.value)),
                )
                for gate in result.gates
            )
            signal = html.escape(str(result.signal.value))
            body = f"""
            <h1>نتیجه اسکن: <span class="status">{signal}</span></h1>
            <section class="card">
              <p>دارایی: <code>{html.escape(asset.raw)}</code></p>
              <p>امتیاز تأیید: {html.escape(str(result.confirmation_score))}</p>
              <p>ریسک برآوردی: {html.escape(str(result.estimated_risk_score))}</p>
              <p>رتبه فرصت: {html.escape(str(result.opportunity_rank))}</p>
              <p>اطمینان پوشش شواهد: {html.escape(str(result.metrics.get("evidence_confidence_pct")))}%</p>
              <p>{html.escape(result.summary_fa)}</p>
            </section>
            <section class="card"><h2>وضعیت فیلترها</h2>
              <table><thead><tr><th>#</th><th>فیلتر</th><th>وضعیت</th></tr></thead><tbody>{gates}</tbody></table>
            </section>
            <p><a href="/">اسکن بعدی</a> · <a href="/history?asset={html.escape(asset.raw)}">تاریخچه نهنگ‌ها</a></p>
            <p class="muted">این خروجی پژوهشی است؛ سفارش، اتصال کیف‌پول یا اجرای معامله انجام نمی‌شود.</p>
            """
            return self._send(200, _page(body, "Scan Result"))
        except Exception as exc:
            message = html.escape(f"{type(exc).__name__}: {exc}")
            return self._send(400, _page(f"<h2>خطا در اسکن</h2><p>{message}</p><a href='/'>بازگشت</a>"))

    def log_message(self, fmt, *args):
        # Keep access logs minimal; never log submitted headlines or social text.
        super().log_message(fmt, *args)


def main():
    port = int(os.getenv("PORT", "10000"))
    host = os.getenv("HOST", "0.0.0.0")
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"CRYPTO SUPER RADAR listening on {host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()

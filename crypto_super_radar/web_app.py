"""Dependency-light Persian web dashboard for on-demand research scans."""
from __future__ import annotations

import html
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .integrated_engine import WhaleAwareConfirmationEngine
from .indicators import observed_price_ranges
from .models import AssetSpec
from .paper_trading import PAPER_ASSETS, PaperTradingBot

ENGINE = WhaleAwareConfirmationEngine()
STORE = ENGINE.whale_store
PAPER_BOT = PaperTradingBot(ENGINE)
JOBS: dict[str, dict] = {}
LAST_SCAN: dict[str, float] = {}
LOCK = threading.RLock()
SCAN_RUNNING = False

# Small, explicitly curated public spot board; it is not a market-cap ranking.
CRYPTO_MARKET_UNIVERSE = (
    ("BTCUSDT", "Bitcoin", "بیت‌کوین"),
    ("ETHUSDT", "Ethereum", "اتریوم"),
    ("SOLUSDT", "Solana", "سولانا"),
    ("BNBUSDT", "BNB", "بی‌ان‌بی"),
    ("XRPUSDT", "XRP", "ریپل"),
    ("DOGEUSDT", "Dogecoin", "دوج‌کوین"),
)
CRYPTO_MARKET_CACHE_TTL_SECONDS = 60
CRYPTO_MARKET_CACHE = {"expires_at": 0.0, "payload": None}
CRYPTO_MARKET_FETCH_LOCK = threading.Lock()


def _page(body: str, title: str = "CRYPTO SUPER RADAR") -> bytes:
    return f"""<!doctype html>
<html lang="fa" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root{{color-scheme:dark;--bg:#07111f;--surface:#0e1b2c;--surface-raised:#14263c;--line:#24374f;--text:#edf4fb;--muted:#98abc0;--teal:#54ded0;--blue:#72a7ff;--green:#53d6a0;--amber:#ffc66d;--red:#ff7b85;--radius:18px}}
*{{box-sizing:border-box}}
body{{font-family:"Segoe UI",Tahoma,Arial,sans-serif;background:radial-gradient(ellipse at 82% -10%,#14334a 0,transparent 34%),var(--bg);color:var(--text);margin:0;padding:32px 20px 48px;line-height:1.65}}
main{{max-width:1120px;margin:auto}}
.app-header{{display:flex;align-items:center;gap:16px;margin:0 0 22px;padding:8px 2px 18px;border-bottom:1px solid var(--line)}}
.brand-mark{{display:grid;place-items:center;width:54px;height:54px;border:1px solid #2a5263;border-radius:17px;background:linear-gradient(145deg,#153445,#102338);font-size:27px;box-shadow:0 10px 30px #0003}}
.brand-copy{{min-width:0;flex:1}}
.brand-copy h1{{margin:1px 0 0;font-size:clamp(1.3rem,3vw,1.85rem);letter-spacing:.035em;line-height:1.25}}
.brand-copy p{{margin:5px 0 0}}
.eyebrow{{color:var(--teal);font-size:.78rem;font-weight:700;letter-spacing:.04em}}
.mode-pill{{display:inline-flex;align-items:center;gap:8px;padding:7px 11px;border:1px solid #2e5360;border-radius:999px;background:#102a34;color:#a8f2e6;font-size:.82rem;white-space:nowrap}}
.mode-dot{{width:8px;height:8px;border-radius:50%;background:var(--teal);box-shadow:0 0 12px #54ded080}}
.card{{background:linear-gradient(145deg,#101e30,#0d1929);border:1px solid var(--line);border-radius:var(--radius);padding:22px;margin:16px 0;box-shadow:0 14px 34px #0002}}
.card h2{{margin:0 0 6px;font-size:1.25rem;letter-spacing:-.01em}}
.card h3{{margin-top:22px}}
.muted{{color:var(--muted)}}
input,button,select{{font:inherit;padding:12px 14px;border-radius:11px;border:1px solid #334a63;background:#091522;color:var(--text);width:100%;box-sizing:border-box}}
input:focus{{outline:2px solid #54ded080;border-color:var(--teal)}}
label{{display:block;margin-top:12px;font-size:.94rem;font-weight:600}}
button{{background:linear-gradient(135deg,#3179e8,#2862c7);border:0;margin-top:12px;cursor:pointer;font-weight:700;transition:filter .15s ease,transform .15s ease}}
button:hover{{filter:brightness(1.12);transform:translateY(-1px)}}
button:focus-visible{{outline:3px solid #54ded080;outline-offset:2px}}
button:disabled{{opacity:.45;cursor:not-allowed;transform:none}}
button.market-scan-button{{width:auto;min-width:132px;margin:0;padding:8px 12px;font-size:.88rem;border:1px solid #315b74;background:#123344;color:#b9f5ed}}
button.market-scan-button:hover{{background:#174355}}
#crypto-market-status{{padding:9px 12px;border:1px solid #1f3448;border-radius:10px;background:#0a1725;font-size:.88rem}}
table{{width:100%;border-collapse:collapse;font-size:.93rem}}
th{{color:var(--muted);font-size:.79rem;font-weight:650;background:#0a1725;white-space:nowrap}}
td,th{{padding:12px 10px;border-bottom:1px solid #203149;text-align:right;vertical-align:middle}}
tbody tr:hover{{background:#ffffff06}}
.numeric{{direction:ltr;text-align:left;font-variant-numeric:tabular-nums;white-space:nowrap}}
.status{{font-weight:bold}} .status-pass{{color:var(--green)}} .status-unknown{{color:var(--amber)}}
code{{direction:ltr;display:inline-block}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#08101d;padding:12px;border-radius:10px;direction:ltr;text-align:left}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}}
.metric{{background:#0a1725;border:1px solid #263a53;border-radius:13px;padding:14px}}
.metric strong{{display:block;font-size:1.2rem;margin-top:6px;direction:ltr;text-align:right;font-variant-numeric:tabular-nums}}
canvas{{width:100%;height:300px;background:#08101d;border-radius:12px}}
.calc-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}}
@media(max-width:680px){{body{{padding:20px 12px 32px}}.app-header{{gap:11px;align-items:flex-start}}.brand-mark{{width:44px;height:44px;border-radius:14px;font-size:23px}}.mode-pill{{font-size:.72rem;padding:5px 8px}}.card{{padding:17px 14px;border-radius:15px}}table{{font-size:.84rem}}td,th{{padding:10px 8px}}button.market-scan-button{{min-width:116px;padding:8px;font-size:.8rem}}}}
</style></head><body><main>{body}</main></body></html>""".encode("utf-8")


def _run_scan(job_id: str, asset: AssetSpec, headline: str, social_post: str) -> None:
    global SCAN_RUNNING
    try:
        result = ENGINE.analyze(
            asset,
            headlines=[headline] if headline else [],
            social_posts=[social_post] if social_post else [],
        )
        candle_frame = getattr(ENGINE.data, "candle_frames", {}).get(asset.raw)
        range_evidence = observed_price_ranges(candle_frame)
        trend_gate = next((gate for gate in result.gates if gate.gate_id == 3), None)
        regime_gate = next((gate for gate in result.gates if gate.gate_id == 2), None)
        candle_records = []
        if candle_frame is not None and not candle_frame.empty:
            required_ohlc = {"open", "high", "low", "close"}
            if required_ohlc.issubset(candle_frame.columns):
                for timestamp, row in candle_frame.tail(120).iterrows():
                    values = [float(row[key]) for key in ("open", "high", "low", "close")]
                    if all(value > 0 for value in values):
                        candle_records.append({
                            "timestamp": timestamp.isoformat() if hasattr(timestamp, "isoformat") else str(timestamp),
                            "open": values[0], "high": values[1], "low": values[2], "close": values[3],
                        })
        payload = {
            "asset": asset.raw,
            "market_overview": {
                "status": range_evidence.get("status", "UNKNOWN"),
                "reason": range_evidence.get("reason"),
                "current_price": range_evidence.get("current_price"),
                "candle_age_hours": range_evidence.get("candle_age_hours"),
                "ranges": range_evidence.get("windows", {}),
                "candles": candle_records,
                "chart_pattern": result.metrics.get("chart_pattern"),
                "asset_regime": (trend_gate.evidence or {}).get("asset_regime") if trend_gate else None,
                "benchmark_regime": (regime_gate.evidence or {}).get("regime") if regime_gate else None,
            },
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
                    "elliott_wave",
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



def _run_paper_tick(job_id: str, asset: AssetSpec) -> None:
    global SCAN_RUNNING
    try:
        result = PAPER_BOT.run_once(asset)
        payload = {"cycle": result, "paper_state": PAPER_BOT.ledger.state()}
        with LOCK:
            JOBS[job_id] = {"status": "completed", "result": payload}
    except Exception as exc:
        with LOCK:
            JOBS[job_id] = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    finally:
        with LOCK:
            SCAN_RUNNING = False


def _crypto_market_board() -> dict:
    """Return a short-lived, free public spot-market board cross-checked by both venues."""
    with CRYPTO_MARKET_FETCH_LOCK:
        now = time.monotonic()
        cached = CRYPTO_MARKET_CACHE["payload"]
        if cached is not None and CRYPTO_MARKET_CACHE["expires_at"] > now:
            return cached

        items = []
        for symbol, name_en, name_fa in CRYPTO_MARKET_UNIVERSE:
            try:
                asset = AssetSpec.parse(f"cex:{symbol}")
                snapshot = ENGINE.data.snapshot(asset)
                items.append({
                    "symbol": symbol,
                    "name_en": name_en,
                    "name_fa": name_fa,
                    "status": "PASS",
                    "price_usd": snapshot.price_usd,
                    "volume_24h_usd": snapshot.volume_24h_usd,
                    "price_discrepancy_pct": snapshot.price_discrepancy_pct,
                    "providers": [snapshot.provider_a, snapshot.provider_b],
                    "received_at_utc": datetime.now(timezone.utc).isoformat(),
                })
            except Exception as exc:
                items.append({
                    "symbol": symbol,
                    "name_en": name_en,
                    "name_fa": name_fa,
                    "status": "UNKNOWN",
                    "reason": f"{type(exc).__name__}: {str(exc)[:180]}",
                })

        payload = {
            "status": "PASS" if all(item["status"] == "PASS" for item in items) else "PARTIAL",
            "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            "cache_ttl_seconds": CRYPTO_MARKET_CACHE_TTL_SECONDS,
            "sources": ["Binance Spot", "Bybit Spot"],
            "items": items,
        }
        CRYPTO_MARKET_CACHE["payload"] = payload
        CRYPTO_MARKET_CACHE["expires_at"] = time.monotonic() + CRYPTO_MARKET_CACHE_TTL_SECONDS
        return payload


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
        if parsed.path == "/api/crypto-markets":
            payload = _crypto_market_board()
            return self._send(200, json.dumps(payload, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8")
        if parsed.path.startswith("/jobs/"):
            job_id = parsed.path.rsplit("/", 1)[-1]
            with LOCK:
                job = JOBS.get(job_id)
            if job is None:
                return self._send(404, b'{"error":"job not found"}', "application/json")
            return self._send(200, json.dumps(job, ensure_ascii=False, default=str).encode(),
                              "application/json; charset=utf-8")
        if parsed.path == "/paper-bot/status":
            payload = PAPER_BOT.ledger.state()
            return self._send(200, json.dumps(payload, ensure_ascii=False).encode(),
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
        <header class="app-header">
          <div class="brand-mark" aria-hidden="true">🐋</div>
          <div class="brand-copy">
            <span class="eyebrow">پایش بازار · دادهٔ عمومی و رایگان</span>
            <h1>CRYPTO SUPER RADAR</h1>
            <p class="muted">تحلیل کمی و Paper Research؛ بدون اتصال کیف‌پول یا ارسال سفارش</p>
          </div>
          <span class="mode-pill"><span class="mode-dot"></span>فقط خواندنی</span>
        </header>
        <section class="card" id="crypto-markets">
          <h2>بازار رمزارزها</h2>
          <p class="muted">قیمت‌های Spot منتخب از APIهای عمومی و رایگان Binance و Bybit؛ نرخ‌ها فقط زمانی نمایش داده می‌شوند که هر دو منبع پاسخ معتبر بدهند. رتبه‌بندی ارزش بازار نیست.</p>
          <p id="crypto-market-status" class="muted" aria-live="polite">در حال دریافت دادهٔ عمومی بازار...</p>
          <div style="overflow-x:auto"><table>
            <thead><tr><th>رمزارز</th><th>نماد</th><th>قیمت مرجع (USD)</th><th>حجم ۲۴ساعته (USD)</th><th>اختلاف منابع</th><th>وضعیت</th><th>اقدام</th></tr></thead>
            <tbody id="crypto-market-rows"><tr><td colspan="7">در حال بارگذاری...</td></tr></tbody>
          </table></div>
          <button type="button" id="crypto-market-refresh">به‌روزرسانی بازار</button>
          <p class="muted">برای آماده‌کردن اسکن، «انتخاب برای اسکن» را بزنید؛ اجرای اسکن فقط با دکمهٔ فرم انجام می‌شود. به‌روزرسانی سمت سرور هر ۶۰ ثانیه cache می‌شود. این بخش هیچ API پولی یا کلید API نمی‌خواهد؛ محدودیت منطقه‌ای و قطع سرویس عمومی ممکن است بعضی ردیف‌ها را UNKNOWN کند.</p>
        </section>
        <script>
        (function(){
          const status=document.getElementById('crypto-market-status');
          const rows=document.getElementById('crypto-market-rows');
          const button=document.getElementById('crypto-market-refresh');
          const fmt=(value,max=8)=>Number(value).toLocaleString(undefined,{maximumFractionDigits:max});
          function cell(tag,value){const node=document.createElement(tag);node.textContent=value??'—';return node;}
          async function refresh(){
            button.disabled=true;status.textContent='در حال دریافت قیمت‌های عمومی...';
            try{
              const response=await fetch('/api/crypto-markets');
              const data=await response.json();
              if(!response.ok)throw new Error('HTTP '+response.status);
              const tableRows=(data.items||[]).map(item=>{
                const tr=document.createElement('tr');
                const values=[item.name_fa+' · '+item.name_en,item.symbol,
                  item.status==='PASS'?fmt(item.price_usd):'—',
                  item.status==='PASS'?fmt(item.volume_24h_usd,2):'—',
                  item.status==='PASS'?fmt(item.price_discrepancy_pct,4)+'%':'—',
                  item.status==='PASS'?'تأیید دو منبع':'نامشخص · UNKNOWN'];
                values.forEach((value,index)=>{const itemCell=cell('td',value);if(index>=2&&index<=4)itemCell.className='numeric';tr.appendChild(itemCell);});
                const actionCell=document.createElement('td');
                const scanButton=document.createElement('button');
                scanButton.type='button';scanButton.className='market-scan-button';
                scanButton.textContent='انتخاب برای اسکن';
                scanButton.disabled=item.status!=='PASS';
                scanButton.addEventListener('click',()=>{
                  const input=document.getElementById('scan-asset');
                  input.value='cex:'+item.symbol;
                  document.getElementById('scan-section').scrollIntoView({behavior:'smooth',block:'start'});
                  input.focus();
                });
                actionCell.appendChild(scanButton);tr.appendChild(actionCell);
                if(item.status!=='PASS')tr.className='status-unknown';
                return tr;
              });
              rows.replaceChildren(...tableRows);
              const received=new Date(data.updated_at_utc).toLocaleString();
              status.textContent='منابع: '+(data.sources||[]).join(' و ')+' · دریافت: '+received+
                ' · وضعیت کلی: '+data.status+' · cache: '+data.cache_ttl_seconds+' ثانیه';
            }catch(error){
              status.textContent='دادهٔ بازار در دسترس نیست: '+String(error.message||error);
            }finally{button.disabled=false;}
          }
          button.addEventListener('click',refresh);refresh();
        })();
        </script>
        <section class="card" id="paper-bot">
          <h2>ربات معامله‌گر آزمایشی (Paper Trading)</h2>
          <p class="muted">ربات فقط LONG در بازار Spot را با همان ۱۲ فیلتر ارزیابی می‌کند؛ هر بار یک چرخه اجرا می‌شود. ریسک هر موقعیت حداکثر ۰٫۵٪ سرمایه، ارزش موقعیت حداکثر ۱۰٪، بدون اهرم و بدون سفارش واقعی است. کارمزد و لغزش نیز در حساب آزمایشی اعمال می‌شوند.</p>
          <form method="post" action="/paper-bot/tick">
            <label>دارایی</label>
            <select name="asset">
              <option value="cex:BTCUSDT">BTC / USDT</option>
              <option value="cex:ETHUSDT">ETH / USDT</option>
              <option value="cex:SOLUSDT">SOL / USDT</option>
            </select>
            <button type="submit">اجرای یک چرخهٔ ربات آزمایشی</button>
          </form>
          <div id="paper-bot-status" class="muted" style="margin-top:12px" aria-live="polite">در حال دریافت وضعیت حساب آزمایشی...</div>
          <p class="muted">برای اجرای دوره‌ای رایگان روی رایانهٔ خود: <code>python scripts/paper_trading_bot.py --asset cex:BTCUSDT --interval-seconds 900</code>. فایل SQLite محلی است؛ سرویس رایگان Render ممکن است هنگام خواب یا راه‌اندازی مجدد فایل‌های محلی را پاک کند.</p>
        </section>
        <script>
        (async function(){
          const box=document.getElementById('paper-bot-status');
          try{
            const r=await fetch('/paper-bot/status');const s=await r.json();
            const a=s.account||{};
            box.textContent='حالت: Paper فقط · موجودی نقدی: '+Number(a.cash_balance_usd||0).toFixed(2)+' USD · ارزش کل حساب: '+Number(a.equity_usd||0).toFixed(2)+' USD · سود/زیان تحقق‌یافته: '+Number(a.realized_pnl_usd||0).toFixed(2)+' USD · موقعیت باز: '+((s.open_position||{}).asset||'ندارد');
          }catch(e){box.textContent='وضعیت حساب آزمایشی در دسترس نیست.';}
        })();
        </script>
        <section class="card" id="scan-section">
          <h2>اسکن دارایی</h2>
          <form method="post" action="/scan">
            <label>بازار و نماد / آدرس</label>
            <input id="scan-asset" name="asset" required value="cex:BTCUSDT" placeholder="cex:BTCUSDT یا base:0x...">
            <p class="muted">نمونه‌ها: cex:BTCUSDT · perp:BTCUSDT · base:0x... · eth:0x... · solana:MINT</p>
            <label>تیتر خبری تکمیلی (اختیاری)</label>
            <input name="headline" placeholder="تیتر خبر">
            <label>متن اجتماعی تکمیلی (اختیاری)</label>
            <input name="social_post" placeholder="متن عمومی شبکه اجتماعی">
            <button type="submit">اجرای اسکن پژوهشی</button>
          </form>
        </section>

        <section class="card" id="market-tools">
          <h2>ماشین‌حساب حرفه‌ای سناریوی Spot</h2>
          <p class="muted">محاسبهٔ سود/زیان فرضی Long بدون اهرم؛ زمان انتخابی فقط افق سناریو است، نه پیش‌بینی زمان سود.</p>
          <div class="calc-grid">
            <div><label for="calc-amount">سرمایه (EUR)</label><input id="calc-amount" type="number" min="0.01" step="0.01" value="10"></div>
            <div><label for="calc-move">تغییر فرضی قیمت (%)</label><input id="calc-move" type="number" min="-99.9" step="0.1" value="10"></div>
            <div><label for="calc-days">افق سناریو (روز)</label><input id="calc-days" type="number" min="1" max="3650" value="14"></div>
            <div><label for="calc-buy-fee">کارمزد خرید (%)</label><input id="calc-buy-fee" type="number" min="0" max="20" step="0.01" value="0.1"></div>
            <div><label for="calc-sell-fee">کارمزد فروش (%)</label><input id="calc-sell-fee" type="number" min="0" max="20" step="0.01" value="0.1"></div>
            <div><label for="calc-buy-slip">لغزش خرید (%)</label><input id="calc-buy-slip" type="number" min="0" max="20" step="0.01" value="0.2"></div>
            <div><label for="calc-sell-slip">لغزش فروش (%)</label><input id="calc-sell-slip" type="number" min="0" max="20" step="0.01" value="0.2"></div>
          </div>
          <div class="grid" style="margin-top:12px">
            <div class="metric">سود/زیان خالص<strong id="calc-pnl">—</strong></div>
            <div class="metric">ارزش نهایی<strong id="calc-value">—</strong></div>
            <div class="metric">بازده خالص<strong id="calc-return">—</strong></div>
            <div class="metric">رشد سربه‌سر<strong id="calc-break-even">—</strong></div>
          </div>
          <h3>مقایسهٔ سناریوهای قیمت</h3><div id="calc-scenarios"></div>
          <p class="muted">فرمول: Vnet = I(1−fb)(1−sb)(1+r)(1−ss)(1−fs). مالیات، تغییر EUR/USD، هزینه برداشت/شبکه و اثر اندازه سفارش خارج از محاسبه‌اند.</p>
          <p class="muted">این ابزار پیش‌بینی، توصیه سرمایه‌گذاری یا سفارش معاملاتی نیست.</p>
        </section>
        <script>
        (function(){
          const ids=['calc-amount','calc-move','calc-days','calc-buy-fee','calc-sell-fee','calc-buy-slip','calc-sell-slip'];
          const el=id=>document.getElementById(id);
          function calc(move){
            const amount=Number(el('calc-amount').value), change=Number(move);
            const bf=Number(el('calc-buy-fee').value)/100, sf=Number(el('calc-sell-fee').value)/100;
            const bs=Number(el('calc-buy-slip').value)/100, ss=Number(el('calc-sell-slip').value)/100;
            const factor=(1-bf)*(1-bs)*(1+change/100)*(1-ss)*(1-sf);
            const net=amount*factor, pnl=net-amount;
            const base=(1-bf)*(1-bs)*(1-ss)*(1-sf);
            return {net,pnl,ret:pnl/amount*100,be:(1/base-1)*100};
          }
          function update(){
            const amount=Number(el('calc-amount').value),move=Number(el('calc-move').value);
            const days=Number(el('calc-days').value);
            const rates=ids.slice(3).map(id=>Number(el(id).value));
            if(!Number.isFinite(amount)||amount<=0||!Number.isFinite(move)||move<=-100||!Number.isFinite(days)||days<1||rates.some(x=>!Number.isFinite(x)||x<0||x>=100)){
              el('calc-pnl').textContent='ورودی نامعتبر';return;
            }
            const out=calc(move),eur=x=>x.toFixed(4)+' €';
            el('calc-pnl').textContent=(out.pnl>=0?'+':'')+eur(out.pnl);
            el('calc-value').textContent=eur(out.net);
            el('calc-return').textContent=(out.ret>=0?'+':'')+out.ret.toFixed(3)+'%';
            el('calc-break-even').textContent=out.be.toFixed(3)+'%';
            const rows=[-20,-10,0,5,10,20].map(v=>{const x=calc(v);return '<tr><td>'+v.toFixed(1)+'%</td><td>'+eur(x.pnl)+'</td><td>'+x.ret.toFixed(3)+'%</td><td>'+days+' روز</td></tr>';}).join('');
            el('calc-scenarios').innerHTML='<table><thead><tr><th>تغییر قیمت</th><th>سود/زیان خالص</th><th>بازده خالص</th><th>افق</th></tr></thead><tbody>'+rows+'</tbody></table>';
          }
          ids.forEach(id=>el(id).addEventListener('input',update));update();
        })();
        </script>
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
        route = urlparse(self.path).path
        if route not in {"/scan", "/paper-bot/tick"}:
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
            if route == "/paper-bot/tick" and (
                asset.market_type != "cex" or asset.symbol not in PAPER_ASSETS
            ):
                raise ValueError("Paper trading accepts only BTC, ETH or SOL spot.")
            headline = form.get("headline", [""])[0].strip() if route == "/scan" else ""
            social_post = form.get("social_post", [""])[0].strip() if route == "/scan" else ""
            job_id = uuid.uuid4().hex
            with LOCK:
                JOBS[job_id] = {"status": "queued"}
                while len(JOBS) > 100:
                    JOBS.pop(next(iter(JOBS)))
            worker = threading.Thread(
                target=_run_paper_tick if route == "/paper-bot/tick" else _run_scan,
                args=(job_id, asset) if route == "/paper-bot/tick" else (job_id, asset, headline, social_post),
                daemon=True,
            )
            worker.start()
            if route == "/paper-bot/tick":
                body = (
                    "<h2>چرخهٔ ربات آزمایشی آغاز شد</h2><p>شناسه: <code>" + job_id + "</code></p>"
                    "<p id='state'>در حال ارزیابی دادهٔ بازار و مدیریت موقعیت کاغذی...</p>"
                    "<pre id='result'>هیچ سفارش واقعی ارسال نمی‌شود.</pre><p><a href='/'>بازگشت</a></p>"
                    "<script>const job='" + job_id + "';async function poll(){try{const r=await fetch('/jobs/'+job);const j=await r.json();document.getElementById('state').textContent=j.status;if(j.status==='completed'){document.getElementById('result').textContent=JSON.stringify(j.result,null,2);return;}if(j.status==='failed'){document.getElementById('result').textContent=j.error;return;}}catch(e){document.getElementById('state').textContent='ارتباط قطع شد؛ وضعیت در /paper-bot/status موجود است';return;}setTimeout(poll,2500);}poll();</script>"
                )
                return self._send(202, _page(body, "Paper Bot Cycle"))
            body = (
                "<h2>اسکن آغاز شد</h2><p>شناسه کار: <code>" + job_id + "</code></p>"
                "<p id='state'>در حال جمع‌آوری داده‌های بازار، نهنگ‌ها و احساسات...</p>"
                "<section class='card'><h3>نمای بازار، کف و سقف</h3><div id='market-summary' class='grid'></div>"
                "<div id='range-table'></div><canvas id='candles' width='1000' height='320'></canvas></section>"
                "<section class='card'><h3>۱۲ فیلتر</h3><div id='gate-table'></div></section>"
                "<pre id='result'>لطفاً این صفحه را باز نگه دارید.</pre><p><a href='/'>بازگشت</a></p>"
                "<script>const job='" + job_id + "';"
                "function cell(tag,text){const e=document.createElement(tag);e.textContent=text??'UNKNOWN';return e;}"
                "function table(container,headers,rows){const t=document.createElement('table'),h=document.createElement('thead'),hr=document.createElement('tr');headers.forEach(v=>hr.appendChild(cell('th',v)));h.appendChild(hr);t.appendChild(h);const b=document.createElement('tbody');rows.forEach(row=>{const tr=document.createElement('tr');row.forEach(v=>tr.appendChild(cell('td',v)));b.appendChild(tr);});t.appendChild(b);container.replaceChildren(t);}"
                "function renderMarket(m){const box=document.getElementById('market-summary');"
                "const items=[['وضعیت دارایی',m.asset_regime],['رژیم معیار',m.benchmark_regime],['قیمت آخر',m.current_price],['سن کندل (ساعت)',m.candle_age_hours],['وضعیت کف/سقف',m.status],['الگوی هندسی',(m.chart_pattern||{}).pattern||'نامشخص'],['وضعیت الگو',(m.chart_pattern||{}).status||'UNKNOWN'],['تأیید شکست با حجم',(m.chart_pattern||{}).breakout_confirmation||'UNKNOWN'],['حجم شکست / میانه ۲۰ کندل',(m.chart_pattern||{}).breakout_volume_ratio||'UNKNOWN']];"
                "box.replaceChildren();items.forEach(([k,v])=>{const d=document.createElement('div');d.className='metric';d.appendChild(cell('span',k));const strong=cell('strong',v);d.appendChild(strong);box.appendChild(d);});"
                "const labels={'7':'۷ روز','30':'۳۰ روز','90':'۹۰ روز','365':'۳۶۵ روز'};"
                "const rows=Object.entries(m.ranges||{}).map(([k,v])=>[labels[k]||k,v.status,v.low,v.high,v.candle_count,v.coverage_pct]);"
                "table(document.getElementById('range-table'),['بازه','وضعیت','کف','سقف','تعداد کندل','پوشش %'],rows);"
                "const canvas=document.getElementById('candles'),ctx=canvas.getContext('2d'),bars=m.candles||[];ctx.clearRect(0,0,canvas.width,canvas.height);"
                "if(!bars.length){ctx.fillStyle='#f59e0b';ctx.fillText('تاریخچه OHLC برای نمودار موجود نیست',20,30);return;}"
                "const lo=Math.min(...bars.map(x=>x.low)),hi=Math.max(...bars.map(x=>x.high)),span=Math.max(hi-lo,1e-12),pad=20,w=(canvas.width-2*pad)/bars.length;"
                "bars.forEach((x,i)=>{const cx=pad+i*w+w/2,y=v=>canvas.height-pad-(v-lo)/span*(canvas.height-2*pad);const up=x.close>=x.open;ctx.strokeStyle=up?'#22c55e':'#ef4444';ctx.fillStyle=ctx.strokeStyle;ctx.beginPath();ctx.moveTo(cx,y(x.high));ctx.lineTo(cx,y(x.low));ctx.stroke();const top=y(Math.max(x.open,x.close)),bottom=y(Math.min(x.open,x.close));ctx.fillRect(cx-w*.32,top,Math.max(1,w*.64),Math.max(1,bottom-top));});const shape=m.chart_pattern||{};if(shape.status==='FORMING'||shape.status==='BREAKOUT_UP'||shape.status==='BREAKOUT_DOWN'){const drawLine=(line,color)=>{if(!line)return;ctx.save();ctx.strokeStyle=color;ctx.lineWidth=2;ctx.setLineDash([7,5]);ctx.beginPath();ctx.moveTo(pad+(line.start_index+.5)*w,y(line.start_price));ctx.lineTo(pad+(line.end_index+.5)*w,y(line.end_price));ctx.stroke();ctx.restore();};drawLine(shape.upper_line,'#ffc66d');drawLine(shape.lower_line,'#72a7ff');(shape.pivots||[]).forEach(p=>{ctx.fillStyle=p.kind==='HIGH'?'#ffc66d':'#72a7ff';ctx.beginPath();ctx.arc(pad+(p.index+.5)*w,y(p.price),4,0,Math.PI*2);ctx.fill();});ctx.fillStyle='#edf4fb';ctx.fillText('Triangle: '+(shape.status||'UNKNOWN')+' / '+(shape.breakout||'NO BREAKOUT'),pad,16);}}"
                "function renderGates(gates){const rows=(gates||[]).map(g=>[g.id,g.name,g.status,g.reason]);table(document.getElementById('gate-table'),['#','فیلتر','وضعیت','دلیل'],rows);}"
                "async function poll(){try{const r=await fetch('/jobs/'+job);const j=await r.json();document.getElementById('state').textContent=j.status;"
                "if(j.status==='completed'){renderMarket(j.result.market_overview||{});renderGates(j.result.gates);document.getElementById('result').textContent=JSON.stringify(j.result,null,2);return;}"
                "if(j.status==='failed'){document.getElementById('result').textContent=j.error;return;}"
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

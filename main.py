import argparse
import logging
import math
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
import requests
from flask import Flask, request

# ---------------------------------------------------------------------------
# RENDER + UPTIME INTEGRATION (Flask Web Server)
# ---------------------------------------------------------------------------
app = Flask(__name__)

cli = logging.getLogger("werkzeug")
cli.setLevel(logging.ERROR)


@app.route("/")
def health_check():
    """UptimeRobot ve Render sağlık kontrolü için yanıt veren uç nokta."""
    return "OK - Hype Bulucu Bot Aktif ve Calisiyor!", 200


# ---------------------------------------------------------------------------
# HIZLI TEKNIK OZET ARACI (/analiz sayfasi)
# ---------------------------------------------------------------------------

def calculate_sma(closes, period):
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def calculate_rsi(closes, period=14):
    """Standart Wilder RSI. closes KRONOLOJIK sirada (en eski once) olmali."""
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_gain == 0 and avg_loss == 0:
        return 50.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _ema_series(values, period):
    k = 2 / (period + 1)
    ema_values = [values[0]]
    for v in values[1:]:
        ema_values.append(v * k + ema_values[-1] * (1 - k))
    return ema_values


def calculate_macd(closes, fast=12, slow=26, signal=9):
    """closes KRONOLOJIK sirada olmali."""
    if len(closes) < slow + signal:
        return None
    ema_fast = _ema_series(closes, fast)
    ema_slow = _ema_series(closes, slow)
    macd_line_series = [f - s for f, s in zip(ema_fast, ema_slow)]
    signal_line_series = _ema_series(macd_line_series, signal)
    macd_line = macd_line_series[-1]
    signal_line = signal_line_series[-1]
    histogram = macd_line - signal_line
    prev_histogram = (
        macd_line_series[-2] - signal_line_series[-2] if len(macd_line_series) > 1 else histogram
    )
    return {"macd": macd_line, "signal": signal_line, "histogram": histogram, "prev_histogram": prev_histogram}


def calculate_atr(highs, lows, closes, period=14):
    """closes/highs/lows KRONOLOJIK sirada olmali."""
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    return sum(trs[-period:]) / period


def find_support_resistance(highs, lows, lookback=30):
    if len(highs) < 2 or len(lows) < 2:
        return None
    recent_highs = highs[-lookback:]
    recent_lows = lows[-lookback:]
    return {"resistance": max(recent_highs), "support": min(recent_lows)}


def fetch_daily_klines(symbol: str, limit: int = 250):
    url = f"{BYBIT_BASE_URL}/v5/market/kline"
    r = requests.get(
        url, params={"category": "linear", "symbol": symbol, "interval": "D", "limit": limit},
        timeout=10,
    )
    data = r.json()
    if data.get("retCode") != 0:
        raise RuntimeError(data.get("retMsg", "Bybit API hatasi"))
    raw = data.get("result", {}).get("list", [])
    if not raw:
        raise RuntimeError(f"'{symbol}' icin veri bulunamadi -- sembol adini kontrol et (orn BTCUSDT).")
    raw = list(reversed(raw))
    closes = [float(c[4]) for c in raw]
    highs = [float(c[2]) for c in raw]
    lows = [float(c[3]) for c in raw]
    turnovers = [float(c[6]) for c in raw]
    return closes, highs, lows, turnovers


def get_funding_rate(symbol: str):
    url = f"{BYBIT_BASE_URL}/v5/market/tickers"
    r = requests.get(url, params={"category": "linear", "symbol": symbol}, timeout=10)
    data = r.json()
    if data.get("retCode") != 0:
        return None
    lst = data.get("result", {}).get("list", [])
    if not lst:
        return None
    try:
        return float(lst[0].get("fundingRate"))
    except (TypeError, ValueError):
        return None


def get_long_short_ratio(symbol: str):
    url = f"{BYBIT_BASE_URL}/v5/market/account-ratio"
    r = requests.get(
        url, params={"category": "linear", "symbol": symbol, "period": "1h", "limit": 1},
        timeout=10,
    )
    data = r.json()
    if data.get("retCode") != 0:
        return None
    lst = data.get("result", {}).get("list", [])
    if not lst:
        return None
    try:
        return {"buy_ratio": float(lst[0]["buyRatio"]), "sell_ratio": float(lst[0]["sellRatio"])}
    except (TypeError, ValueError, KeyError):
        return None


def compute_volume_comparison(turnovers):
    if len(turnovers) < 6:
        return None
    today = turnovers[-1]
    history = turnovers[:-1]
    avg_before = sum(history) / len(history)
    if avg_before <= 0:
        return None
    return {"today": today, "avg_before": avg_before, "ratio": today / avg_before}


def get_coingecko_market_data(symbol: str):
    base = symbol.replace("USDT", "").strip()
    if not base:
        return None
    try:
        search_url = "https://api.coingecko.com/api/v3/search"
        r = requests.get(search_url, params={"query": base}, headers=COINGECKO_HEADERS, timeout=8)
        data = r.json()
        coins = data.get("coins", [])
        exact_matches = [c for c in coins if c.get("symbol", "").upper() == base.upper()]
        candidate = exact_matches[0] if exact_matches else (coins[0] if coins else None)
        if not candidate:
            return None
        coin_id = candidate.get("id")
        if not coin_id:
            return None

        detail_url = f"https://api.coingecko.com/api/v3/coins/{coin_id}"
        r2 = requests.get(
            detail_url,
            params={"localization": "false", "tickers": "false", "market_data": "true",
                    "community_data": "false", "developer_data": "false"},
            headers=COINGECKO_HEADERS,
            timeout=8,
        )
        detail = r2.json()
        market_data = detail.get("market_data", {})
        market_cap = market_data.get("market_cap", {}).get("usd")
        circulating = market_data.get("circulating_supply")
        rank = detail.get("market_cap_rank")
        if market_cap is None:
            return None
        return {"market_cap": market_cap, "circulating_supply": circulating, "rank": rank, "coingecko_id": coin_id}
    except Exception:
        return None


_trending_cache = {"symbols": set(), "fetched_at": 0}


def get_coingecko_trending(cache_seconds: int = 600):
    now = time.time()
    if now - _trending_cache["fetched_at"] < cache_seconds and _trending_cache["symbols"]:
        return _trending_cache["symbols"]

    try:
        url = "https://api.coingecko.com/api/v3/search/trending"
        r = requests.get(url, headers=COINGECKO_HEADERS, timeout=8)
        data = r.json()
        coins = data.get("coins", [])
        symbols = set()
        for c in coins:
            item = c.get("item", {})
            sym = item.get("symbol", "").upper()
            if sym:
                symbols.add(sym)
        if symbols:
            _trending_cache["symbols"] = symbols
            _trending_cache["fetched_at"] = now
        return symbols
    except Exception as e:
        logging.error(f"[CoinGecko Trending] hata: {e}")
        return _trending_cache["symbols"]


def generate_technical_summary(symbol: str):
    closes, highs, lows, turnovers = fetch_daily_klines(symbol)
    last_price = closes[-1]

    rsi = calculate_rsi(closes)
    ma20 = calculate_sma(closes, 20)
    ma50 = calculate_sma(closes, 50)
    ma200 = calculate_sma(closes, 200)
    macd = calculate_macd(closes)
    atr = calculate_atr(highs, lows, closes)
    levels = find_support_resistance(highs, lows, lookback=30)
    volume_cmp = compute_volume_comparison(turnovers)

    try:
        cvd_ratio = get_cvd_buy_ratio(symbol)
    except Exception:
        cvd_ratio = None
    try:
        funding_rate = get_funding_rate(symbol)
    except Exception:
        funding_rate = None
    try:
        long_short = get_long_short_ratio(symbol)
    except Exception:
        long_short = None
    try:
        market_data = get_coingecko_market_data(symbol)
    except Exception:
        market_data = None
    try:
        trending_symbols = get_coingecko_trending()
        base_symbol = symbol.replace("USDT", "").strip().upper()
        is_trending = base_symbol in trending_symbols
    except Exception:
        is_trending = False

    notlar = []

    if rsi is not None:
        if rsi >= 70:
            notlar.append(f"RSI {rsi:.0f} -- aşırı alım bölgesinde, kısa vadeli geri çekilme riski artmış olabilir.")
        elif rsi <= 30:
            notlar.append(f"RSI {rsi:.0f} -- aşırı satım bölgesinde, tepki yükselişi görülebilir.")
        else:
            notlar.append(f"RSI {rsi:.0f} -- nötr bölgede, belirgin bir aşırılık yok.")

    if ma20 and ma50 and ma200:
        if last_price > ma20 > ma50 > ma200:
            notlar.append("Fiyat MA20/MA50/MA200'ün üzerinde ve sıralama yükseliş trendine işaret ediyor (güçlü trend).")
        elif last_price < ma20 < ma50 < ma200:
            notlar.append("Fiyat MA20/MA50/MA200'ün altında ve sıralama düşüş trendine işaret ediyor (zayıf trend).")
        elif last_price > ma200:
            notlar.append("Fiyat uzun vadeli ortalamanın (MA200) üzerinde ama kısa vadeli ortalamalarla karışık -- net bir trend yok.")
        else:
            notlar.append("Fiyat uzun vadeli ortalamanın (MA200) altında, genel görünüm zayıf.")

    if macd:
        if macd["histogram"] > 0 and macd["prev_histogram"] <= 0:
            notlar.append("MACD az önce pozitif kesişim yaptı -- yükseliş momentumu yeni başlıyor olabilir.")
        elif macd["histogram"] < 0 and macd["prev_histogram"] >= 0:
            notlar.append("MACD az önce negatif kesişim yaptı -- düşüş momentumu yeni başlıyor olabilir.")
        elif macd["histogram"] > 0:
            notlar.append("MACD pozitif -- yükseliş momentumu devam ediyor.")
        else:
            notlar.append("MACD negatif -- düşüş momentumu devam ediyor.")

    if levels:
        dist_to_resistance = (levels["resistance"] - last_price) / last_price * 100
        dist_to_support = (last_price - levels["support"]) / last_price * 100
        notlar.append(
            f"Son 30 günün direnci ~{levels['resistance']:.6g} (fiyatın %{dist_to_resistance:.1f} üzerinde), "
            f"desteği ~{levels['support']:.6g} (fiyatın %{dist_to_support:.1f} altında)."
        )
    if atr:
        atr_pct = atr / last_price * 100
        notlar.append(f"Ortalama günlük volatilite (ATR): ~%{atr_pct:.1f} -- bu, günlük 'normal' dalgalanma büyüklüğü.")

    if volume_cmp:
        notlar.append(
            f"Bugünkü ciro, önceki günlerin ortalamasının {volume_cmp['ratio']:.2f} katı "
            f"({'artış' if volume_cmp['ratio'] >= 1 else 'azalış'})."
        )

    if cvd_ratio is not None:
        if cvd_ratio >= 0.60:
            notlar.append(f"CVD: son işlemlerin %{cvd_ratio*100:.0f}'i alış -- alıcı baskısı güçlü.")
        elif cvd_ratio <= 0.45:
            notlar.append(f"CVD: son işlemlerin sadece %{cvd_ratio*100:.0f}'i alış -- satıcı baskısı ağır basıyor.")
        else:
            notlar.append(f"CVD: alış oranı %{cvd_ratio*100:.0f} -- dengeli, belirgin bir baskı yok.")

    if funding_rate is not None:
        funding_pct = funding_rate * 100
        if funding_pct >= 0.05:
            notlar.append(f"Funding rate %{funding_pct:.3f} -- long tarafı belirgin şekilde kalabalık, ani bir long tasfiyesi riski artmış olabilir.")
        elif funding_pct <= -0.05:
            notlar.append(f"Funding rate %{funding_pct:.3f} -- short tarafı belirgin şekilde kalabalık, ani bir short squeeze riski artmış olabilir.")
        else:
            notlar.append(f"Funding rate %{funding_pct:.3f} -- nötr, aşırı bir pozisyon yığılması yok.")

    if long_short is not None:
        notlar.append(
            f"Kullanıcı Long/Short oranı: %{long_short['buy_ratio']*100:.0f} long / "
            f"%{long_short['sell_ratio']*100:.0f} short."
        )

    if market_data is not None:
        rank_str = f" (piyasa değeri sıralaması: #{market_data['rank']})" if market_data.get("rank") else ""
        notlar.append(f"Market cap: ~${market_data['market_cap']:,.0f}{rank_str}.")

    if is_trending:
        notlar.append("🔥 Bu coin şu anda CoinGecko'nun 'Trending' (en çok aranan) listesinde -- "
                      "hem hacim hem genel arama ilgisi aynı anda yükseliyor, bu çapraz teyit güçlü bir sinyal.")

    return {
        "symbol": symbol,
        "last_price": last_price,
        "rsi": rsi,
        "ma20": ma20,
        "ma50": ma50,
        "ma200": ma200,
        "macd": macd,
        "atr": atr,
        "levels": levels,
        "volume_cmp": volume_cmp,
        "cvd_ratio": cvd_ratio,
        "funding_rate": funding_rate,
        "long_short": long_short,
        "market_data": market_data,
        "is_trending": is_trending,
        "notlar": notlar,
    }


ANALIZ_SAYFA_TEMPLATE = """
<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hizli Teknik Ozet</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; background:#0b0f14; color:#e6edf3; margin:0; padding:16px; }}
  h1 {{ font-size: 20px; }}
  .card {{ background:#161b22; border:1px solid #30363d; border-radius:10px; padding:14px 16px; margin-bottom:12px; }}
  input[type=text] {{ background:#0d1117; border:1px solid #30363d; color:#e6edf3; padding:10px; border-radius:8px; font-size:16px; width:60%; }}
  button {{ background:#238636; color:white; border:none; padding:10px 18px; border-radius:8px; font-size:16px; }}
  .row {{ display:flex; justify-content:space-between; padding:4px 0; font-size:14px; }}
  .label {{ color:#8b949e; }}
  .err {{ color:#f85149; }}
  .not-item {{ font-size:14px; margin:6px 0; line-height:1.5; }}
  .disclaimer {{ color:#8b949e; font-size:12px; margin-top:16px; }}
</style>
</head>
<body>
  <h1>🔍 Hizli Teknik Ozet</h1>
  <form method="get" action="/analiz">
    <input type="text" name="symbol" placeholder="orn BTCUSDT" value="{symbol_value}">
    <button type="submit">Incele</button>
  </form>
  <div style="margin-top:16px;">
  {results}
  </div>
  <div class="disclaimer">
    Bu sayfa yatirim tavsiyesi degildir, sadece teknik gostergelerin okunabilir
    bir ozetidir. Karar tamamen sanadir.
  </div>
</body>
</html>
"""


@app.route("/analiz")
def analiz_page():
    symbol = request.args.get("symbol", "").strip().upper()
    results_html = ""

    if symbol:
        try:
            r = generate_technical_summary(symbol)
            notlar_html = "".join(f'<div class="not-item">• {n}</div>' for n in r["notlar"])

            cvd_row = ""
            if r.get("cvd_ratio") is not None:
                cvd_row = f'<div class="row"><span class="label">CVD (Alis Orani)</span><span>%{r["cvd_ratio"]*100:.0f}</span></div>'

            funding_row = ""
            if r.get("funding_rate") is not None:
                funding_row = f'<div class="row"><span class="label">Funding Rate</span><span>%{r["funding_rate"]*100:.3f}</span></div>'

            ls_row = ""
            if r.get("long_short") is not None:
                ls = r["long_short"]
                ls_row = f'<div class="row"><span class="label">Long/Short</span><span>%{ls["buy_ratio"]*100:.0f} / %{ls["sell_ratio"]*100:.0f}</span></div>'

            vol_row = ""
            if r.get("volume_cmp") is not None:
                vol_row = f'<div class="row"><span class="label">Hacim Orani (bugun/ortalama)</span><span>{r["volume_cmp"]["ratio"]:.2f}x</span></div>'

            mcap_row = ""
            if r.get("market_data") is not None:
                mcap_row = f'<div class="row"><span class="label">Market Cap</span><span>${r["market_data"]["market_cap"]:,.0f}</span></div>'

            trending_row = ""
            if r.get("is_trending"):
                trending_row = '<div class="row"><span class="label">CoinGecko Trending</span><span>🔥 EVET</span></div>'

            results_html = f"""
            <div class="card">
              <div class="row"><span class="label">Sembol</span><span>{r['symbol']}</span></div>
              <div class="row"><span class="label">Guncel Fiyat</span><span>{r['last_price']}</span></div>
              <div class="row"><span class="label">RSI (14)</span><span>{r['rsi']:.1f}</span></div>
              <div class="row"><span class="label">MA20 / MA50 / MA200</span><span>{r['ma20']:.6g} / {r['ma50']:.6g} / {r['ma200']:.6g}</span></div>
              <div class="row"><span class="label">MACD Histogram</span><span>{r['macd']['histogram']:.6g}</span></div>
              {vol_row}
              {cvd_row}
              {funding_row}
              {ls_row}
              {mcap_row}
              {trending_row}
            </div>
            <div class="card">
              <b>Teknik Ozet:</b>
              {notlar_html}
            </div>
            """
        except Exception as e:
            results_html = f'<div class="card err">Hata: {e}</div>'

    return ANALIZ_SAYFA_TEMPLATE.format(symbol_value=symbol, results=results_html)


_seed_status = {"running": False, "done": 0, "total": 0, "started_at": None, "finished_at": None}


def _run_baseline_seeding():
    global _seed_status
    _seed_status["running"] = True
    _seed_status["done"] = 0
    _seed_status["started_at"] = datetime.now(timezone.utc).isoformat()
    logging.info("[Seed] Gercek gecmis veriyle baseline doldurma basladi...")

    tickers = get_bybit_tickers()
    symbols = [t["symbol"] for t in tickers if t.get("symbol", "").endswith("USDT")]
    _seed_status["total"] = len(symbols)

    conn = sqlite3.connect(DB_PATH)
    now = datetime.now(timezone.utc)
    ok_count = 0

    for symbol in symbols:
        try:
            url = f"{BYBIT_BASE_URL}/v5/market/kline"
            r = requests.get(
                url, params={"category": "linear", "symbol": symbol, "interval": "D", "limit": 14},
                timeout=10,
            )
            data = r.json()
            if data.get("retCode") != 0:
                continue
            klines = data.get("result", {}).get("list", [])
            if len(klines) < 5:
                continue

            turnovers = [float(k[6]) for k in klines if float(k[6]) > 0]
            if len(turnovers) < 5:
                continue
            avg_turnover = sum(turnovers) / len(turnovers)

            n_rows = BASELINE_MIN_SAMPLES + 10
            for i in range(n_rows):
                ts = (
                    now - timedelta(hours=BASELINE_EXCLUDE_RECENT_HOURS + 0.25)
                    - timedelta(minutes=i * 15)
                ).isoformat()
                conn.execute(
                    """INSERT INTO hype_observations
                       (timestamp, inst_id, last_price, change_24h_pct, turnover_24h,
                        power_score, freshness_ratio, final_score, notified)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (ts, symbol, 0, 0, avg_turnover, None, 1.0, None, 0),
                )
            conn.commit()
            ok_count += 1
        except Exception as e:
            logging.error(f"[Seed] {symbol} hata: {e}")

        _seed_status["done"] += 1
        time.sleep(0.1)

    conn.close()
    _seed_status["running"] = False
    _seed_status["finished_at"] = datetime.now(timezone.utc).isoformat()
    logging.info(f"[Seed] Tamamlandi. {ok_count}/{len(symbols)} sembol icin baseline dolduruldu.")


@app.route("/seed_baseline")
def seed_baseline_route():
    key = request.args.get("key", "")
    if key != SEED_SECRET_KEY:
        return "Yetkisiz -- dogru 'key' parametresini gir.", 403

    if _seed_status["running"]:
        return "Zaten calisiyor. Ilerlemeyi /seed_status adresinden takip et.", 200

    thread = threading.Thread(target=_run_baseline_seeding, daemon=True)
    thread.start()
    return (
        "Baseline doldurma islemi arka planda BASLADI (birkac dakika surebilir). "
        "Ilerlemeyi /seed_status adresinden takip edebilirsin.",
        200,
    )


@app.route("/seed_status")
def seed_status_route():
    s = _seed_status
    if s["started_at"] is None:
        return "Henuz hic calistirilmadi."
    durum = "CALISIYOR" if s["running"] else "TAMAMLANDI"
    return f"Durum: {durum} | Ilerleme: {s['done']}/{s['total']} | Baslangic: {s['started_at']} | Bitis: {s['finished_at']}"


@app.route("/test_telegram")
def test_telegram_route():
    key = request.args.get("key", "")
    if key != SEED_SECRET_KEY:
        return "Yetkisiz -- dogru 'key' parametresini gir.", 403

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return "HATA: TELEGRAM_BOT_TOKEN veya TELEGRAM_CHAT_ID tanimli degil.", 200

    test_msg = (
        "🧪 *TEST MESAJI*\n\n"
        "Bu, /test_telegram adresinden manuel olarak tetiklenen bir test "
        "mesajidir. Bu mesaji goruyorsan, Telegram baglantisi calisiyor demektir."
    )
    send_telegram_alert(test_msg)
    return "Test mesaji gonderildi. Telegram'ini kontrol et. Gelmezse Render/VPS loglarinda 'Telegram API Hatasi' ara.", 200


def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


# ---------------------------------------------------------------------------
# KONFİGÜRASYON VE SABİTLER
# ---------------------------------------------------------------------------
DB_PATH = "hype_history.db"

BYBIT_BASE_URL = "https://api.bytick.com"

SEED_SECRET_KEY = os.environ.get("SEED_SECRET_KEY", "degistir-bu-anahtari")

COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY", "")
COINGECKO_HEADERS = {"x-cg-demo-api-key": COINGECKO_API_KEY} if COINGECKO_API_KEY else {}

SCAN_INTERVAL_SECONDS = 900

REPORT_INTERVAL_SECONDS = 4 * 3600
REPORT_MIN_MOVE_PCT = 10.0
REPORT_MAX_ITEMS_SHOWN = 15

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

MIN_TURNOVER_24H = 300_000.0

BASELINE_MIN_SAMPLES = 20
BASELINE_EXCLUDE_RECENT_HOURS = 2.0
FRESHNESS_CHECK_VOLUME_RATIO = 2.5

ALERT_POWER_SCORE_THRESHOLD = 6.0

BIG_MOVE_PRICE_CHANGE_PCT = 40.0

ALERT_COOLDOWN_HOURS = 6.0

BREAKOUT_LOOKBACK_HOURS = 10
BREAKOUT_RANGE_MAX_PCT = 12.0
BREAKOUT_VOLUME_MULTIPLIER = 2.0
BREAKOUT_PREFILTER_VOLUME_RATIO = 1.5
BREAKOUT_PREFILTER_PRICE_CHANGE_PCT = 5.0
BREAKOUT_ALERT_COOLDOWN_HOURS = 4.0


OI_CHANGE_STRONG_PCT = 5.0
OI_CHANGE_WEAK_NEGATIVE_PCT = -3.0
CVD_BUY_STRONG = 0.60
CVD_SELL_STRONG = 0.45
EXHAUSTION_POSITION_THRESHOLD = 0.90

# --- YENI (Madde 1): Tukenme/ters donus bolgesinde ana sinyali TAMAMEN BASTIR ---
# RSI asiri + hareket eski + CVD uyumsuzlugu ayni anda varsa, bu "giris" degil
# "cikis/tersine donus" bolgesidir -- Burakcan'in notuna gore boyle bir durumda
# "HYPE" diye bildirim gondermek yaniltici, o yuzden TAMAMEN BASTIRILIYOR
# (hem skor yolu hem buyuk hareket yolu icin).

# --- YENI (Madde 3): Funding rate esikleri (ana taramada da kullanilacak) ---
FUNDING_EXTREME_POS_PCT = 0.05   # /analiz sayfasindaki esikle tutarli
FUNDING_EXTREME_NEG_PCT = -0.05
FUNDING_HISTORY_LOOKBACK_HOURS = 6.0  # "son 4-8 saatteki degisim" icin kullanilan pencere

# --- YENI (Madde 4): "Guclu Setup" vurgusu icin esikler ---
STRONG_SETUP_RSI_MAX = 60.0
STRONG_SETUP_CVD_MIN = 0.65

# --- YENI: OI Coklu Zaman Dilimi Teyidi (Burakcan'in notuna gore) ---
# Scalp/gun ici olcek: 15dk/1sa'da OI degisimi %5-15 arasi 'onemli' sayilir.
# Swing/trend olcegi: 4sa/1gun'de OI degisimi %30+ 'onemli' sayilir.
# Ikisinden BIRI yeterli -- coin'in 'scalp' mi 'swing' mi bir hareket
# icinde oldugunu ayirt eder.
OI_SCALP_MIN_PCT = 5.0
OI_SCALP_MAX_PCT = 15.0
OI_SWING_MIN_PCT = 30.0

# --- YENI: Ana "Giris Firsati" kapisi -- ARTIK ana hype sinyali icin
# ZORUNLU sartlar (sadece bilgi/vurgu degil). Kararlastirildigi gibi bu,
# sistemi LONG/al firsatlarina odaklaniyor.
ENTRY_GATE_RSI_MAX = STRONG_SETUP_RSI_MAX
ENTRY_GATE_CVD_MIN = STRONG_SETUP_CVD_MIN

# --- YENI (Madde 6): Funding asiriligi bazli AYRI, paralel bir tarama ---
FUNDING_SCAN_EXTREME_PCT = 0.05          # bu yuzdenin uzerindeki/altindaki funding 'asiri' sayilir
FUNDING_SCAN_MIN_PRICE_MOVE_PCT = 10.0    # fiyat da bu kadar hareket etmis olmali
FUNDING_SCAN_COOLDOWN_HOURS = 6.0         # ayri bir cooldown -- diger sinyallerden bagimsiz

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# ---------------------------------------------------------------------------
# VERİTABANI İŞLEMLERİ (SQLite)
# ---------------------------------------------------------------------------


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS hype_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            inst_id TEXT NOT NULL,
            last_price REAL,
            change_24h_pct REAL,
            turnover_24h REAL,
            power_score REAL,
            freshness_ratio REAL,
            final_score REAL,
            notified INTEGER DEFAULT 0
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_inst_time ON hype_observations (inst_id, timestamp)"
    )

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS breakout_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            inst_id TEXT NOT NULL,
            direction TEXT NOT NULL
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_breakout_inst_time ON breakout_alerts (inst_id, timestamp)"
    )

    # YENI (Madde 6): Funding asiriligi sinyali icin AYRI bir tablo --
    # kirilim tablosuyla ayni desen, sifir risk.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS funding_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            inst_id TEXT NOT NULL
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_funding_inst_time ON funding_alerts (inst_id, timestamp)"
    )

    for col_def in [
        "oi_value REAL",
        "oi_change_pct REAL",
        "cvd_buy_ratio REAL",
        "position_in_range REAL",
        "yorum TEXT",
        "volume_ratio REAL",
        "funding_rate REAL",
    ]:
        try:
            cursor.execute(f"ALTER TABLE hype_observations ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass

    conn.commit()
    conn.close()


def record_observation(data: dict):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO hype_observations (
            timestamp, inst_id, last_price, change_24h_pct, turnover_24h,
            power_score, freshness_ratio, final_score, notified,
            oi_value, oi_change_pct, cvd_buy_ratio, position_in_range, yorum,
            volume_ratio, funding_rate
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            data["timestamp"],
            data["inst_id"],
            data["last_price"],
            data["change_24h_pct"],
            data["turnover_24h"],
            data["power_score"],
            data["freshness_ratio"],
            data["final_score"],
            data["notified"],
            data.get("oi_value"),
            data.get("oi_change_pct"),
            data.get("cvd_buy_ratio"),
            data.get("position_in_range"),
            data.get("yorum"),
            data.get("volume_ratio"),
            data.get("funding_rate"),
        ),
    )
    conn.commit()
    conn.close()


def is_recently_notified(inst_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cutoff_time = (
        datetime.now(timezone.utc) - timedelta(hours=ALERT_COOLDOWN_HOURS)
    ).isoformat()
    cursor.execute(
        """
        SELECT COUNT(*) FROM hype_observations
        WHERE inst_id = ? AND notified = 1 AND timestamp >= ?
    """,
        (inst_id, cutoff_time),
    )
    count = cursor.fetchone()[0]
    conn.close()
    return count > 0


def get_previous_oi(inst_id: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=23)).isoformat()
    cursor.execute(
        """
        SELECT oi_value FROM hype_observations
        WHERE inst_id = ? AND oi_value IS NOT NULL AND timestamp <= ?
        ORDER BY timestamp DESC LIMIT 1
    """,
        (inst_id, cutoff),
    )
    row = cursor.fetchone()
    conn.close()
    return row[0] if row and row[0] else None


def get_oi_at_least_hours_ago(inst_id: str, hours_ago: float):
    """
    YENI: get_previous_oi'nin genellestirilmis hali -- herhangi bir saat
    onceki OI degerini bulur (en az 'hours_ago' kadar eski, en yakin kayit).
    OI coklu zaman dilimi teyidi (Madde: scalp/swing ayrimi) icin kullanilir.
    """
    conn = sqlite3.connect(DB_PATH)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    row = conn.execute(
        """
        SELECT oi_value FROM hype_observations
        WHERE inst_id = ? AND oi_value IS NOT NULL AND timestamp <= ?
        ORDER BY timestamp DESC LIMIT 1
        """,
        (inst_id, cutoff),
    ).fetchone()
    conn.close()
    return row[0] if row and row[0] else None


def check_oi_multiframe_confirmation(inst_id: str, current_oi):
    """
    YENI: Burakcan'in notuna gore -- OI degisimini islem tarzina uygun
    zaman diliminde degerlendirir:
      - Scalp/gun ici: 1 saatlik OI degisimi %5-15 arasindaysa teyit sayilir.
      - Swing/trend: 4 saatlik OI degisimi %30+ ise teyit sayilir.
    Ikisinden BIRI yeterli. Hicbiri saglanmiyorsa 'passed': False doner.

    Donus: {'passed': bool, 'scale': 'scalp'/'swing'/None, 'detail': str}
    """
    if current_oi is None:
        return {"passed": False, "scale": None, "detail": "Guncel OI verisi yok."}

    oi_1h_ago = get_oi_at_least_hours_ago(inst_id, 1.0)
    oi_4h_ago = get_oi_at_least_hours_ago(inst_id, 4.0)

    scalp_change = None
    if oi_1h_ago and oi_1h_ago > 0:
        scalp_change = (current_oi - oi_1h_ago) / oi_1h_ago * 100.0

    swing_change = None
    if oi_4h_ago and oi_4h_ago > 0:
        swing_change = (current_oi - oi_4h_ago) / oi_4h_ago * 100.0

    scalp_ok = scalp_change is not None and OI_SCALP_MIN_PCT <= scalp_change <= OI_SCALP_MAX_PCT
    swing_ok = swing_change is not None and swing_change >= OI_SWING_MIN_PCT

    if scalp_ok:
        return {
            "passed": True, "scale": "scalp",
            "detail": f"1sa OI degisimi %{scalp_change:.1f} (scalp araligi %{OI_SCALP_MIN_PCT:.0f}-{OI_SCALP_MAX_PCT:.0f})",
        }
    if swing_ok:
        return {
            "passed": True, "scale": "swing",
            "detail": f"4sa OI degisimi %{swing_change:.1f} (swing esigi %{OI_SWING_MIN_PCT:.0f}+)",
        }

    parts = []
    if scalp_change is not None:
        parts.append(f"1sa: %{scalp_change:.1f}")
    if swing_change is not None:
        parts.append(f"4sa: %{swing_change:.1f}")
    detail = "OI coklu-zaman-dilimi teyidi yok" + (f" ({', '.join(parts)})" if parts else " (yeterli gecmis yok)")
    return {"passed": False, "scale": None, "detail": detail}


def get_previous_funding_rate(inst_id: str, hours_ago: float = FUNDING_HISTORY_LOOKBACK_HOURS):
    """
    YENI (Madde 3): Yaklasik 'hours_ago' saat once kaydedilmis funding rate
    degerini bulur -- "son 4-8 saatteki degisim" hesaplamak icin. Kendi
    veritabanimizdan, ayni get_previous_oi mantigiyla.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours_ago + 0.5)).isoformat()
    cursor.execute(
        """
        SELECT funding_rate FROM hype_observations
        WHERE inst_id = ? AND funding_rate IS NOT NULL AND timestamp <= ?
        ORDER BY timestamp DESC LIMIT 1
    """,
        (inst_id, cutoff),
    )
    row = cursor.fetchone()
    conn.close()
    return row[0] if row and row[0] is not None else None


def get_average_turnover(inst_id: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=BASELINE_EXCLUDE_RECENT_HOURS)
    ).isoformat()
    cursor.execute(
        """
        SELECT turnover_24h FROM hype_observations
        WHERE inst_id = ? AND turnover_24h IS NOT NULL AND timestamp <= ?
        ORDER BY timestamp DESC LIMIT 200
    """,
        (inst_id, cutoff),
    )
    rows = [r[0] for r in cursor.fetchall() if r[0]]
    conn.close()
    if len(rows) < BASELINE_MIN_SAMPLES:
        return None
    return sum(rows) / len(rows)


def get_symbol_history(inst_id: str, limit: int = 10):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT timestamp, last_price, change_24h_pct, turnover_24h, power_score,
               freshness_ratio, final_score, oi_change_pct, cvd_buy_ratio, yorum,
               volume_ratio
        FROM hype_observations
        WHERE inst_id = ?
        ORDER BY timestamp DESC
        LIMIT ?
    """,
        (inst_id, limit),
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# TELEGRAM BİLDİRİM FONKSİYONU
# ---------------------------------------------------------------------------


def send_telegram_alert(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("Telegram Token veya Chat ID bulunamadı. Bildirim atlanıyor.")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
    }

    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code != 200:
            logging.error(f"Telegram API Hatası: {res.text}")
    except Exception as e:
        logging.error(f"Telegram bildirimi gönderilirken hata oluştu: {e}")


# ---------------------------------------------------------------------------
# BYBIT BORSASI VERİ ÇEKME VE HESAPLAMA MANTIĞI
# ---------------------------------------------------------------------------


def get_bybit_tickers():
    url = f"{BYBIT_BASE_URL}/v5/market/tickers"
    try:
        response = requests.get(url, params={"category": "linear"}, timeout=15)
        data = response.json()
        if data.get("retCode") == 0:
            return data.get("result", {}).get("list", [])
        else:
            logging.error(f"Bybit Tickers API hatasi: {data.get('retMsg')}")
    except Exception as e:
        logging.error(f"Bybit Tickers çekilirken hata: {e}")
    return []


def get_all_open_interest(tickers):
    result = {}
    for item in tickers:
        try:
            symbol = item.get("symbol")
            oi_value = float(item.get("openInterestValue") or item.get("openInterest") or 0)
            if symbol:
                result[symbol] = oi_value
        except (TypeError, ValueError):
            continue
    return result


def fetch_recent_klines_short(inst_id: str, interval: str = "15", limit: int = 30):
    try:
        url = f"{BYBIT_BASE_URL}/v5/market/kline"
        r = requests.get(
            url, params={"category": "linear", "symbol": inst_id, "interval": interval, "limit": limit},
            timeout=10,
        )
        data = r.json()
        if data.get("retCode") != 0:
            return None, None
        raw = data.get("result", {}).get("list", [])
        if len(raw) < 5:
            return None, None
        raw = list(reversed(raw))
        closes = [float(c[4]) for c in raw]
        turnovers = [float(c[6]) for c in raw]
        return closes, turnovers
    except Exception as e:
        logging.error(f"{inst_id} kisa vadeli mum verisi hatasi: {e}")
        return None, None


def find_breakout_start_minutes_ago(turnovers, bar_minutes: int = 15, volume_multiplier: float = 2.5, baseline_bars: int = 8):
    if len(turnovers) < baseline_bars + 3:
        return None
    baseline_segment = turnovers[:baseline_bars]
    baseline_avg = sum(baseline_segment) / len(baseline_segment)
    if baseline_avg <= 0:
        return None
    threshold = baseline_avg * volume_multiplier

    start_idx = None
    for i in range(baseline_bars, len(turnovers)):
        if turnovers[i] >= threshold:
            start_idx = i
            break
    if start_idx is None:
        return None

    bars_since_start = len(turnovers) - 1 - start_idx
    return bars_since_start * bar_minutes


def classify_freshness(inst_id: str):
    closes, turnovers = fetch_recent_klines_short(inst_id, interval="15", limit=30)

    if closes is None or len(closes) < 15:
        return {
            "label": "BELIRSIZ",
            "short_rsi": None,
            "breakout_age_minutes": None,
            "aciklama": "Kisa vadeli veri yetersiz, taze/uzamis ayrimi yapilamadi.",
        }

    short_rsi = calculate_rsi(closes, period=14)
    breakout_age = find_breakout_start_minutes_ago(turnovers, bar_minutes=15)

    is_rsi_overbought = short_rsi is not None and short_rsi >= 75
    is_rsi_oversold = short_rsi is not None and short_rsi <= 25
    is_rsi_extreme = is_rsi_overbought or is_rsi_oversold
    rsi_extreme_text = "asiri alim" if is_rsi_overbought else "asiri satim"
    is_old = breakout_age is not None and breakout_age >= 120
    is_fresh = breakout_age is not None and breakout_age <= 45

    if is_rsi_extreme and is_old:
        label = "UZAMIS"
        aciklama = (
            f"Kisa vadeli RSI {short_rsi:.0f} ({rsi_extreme_text}) ve hareket ~{breakout_age} dakikadir "
            f"suruyor -- bu, zaten uzamis bir hareket, tukenme riski yuksek olabilir."
        )
    elif is_fresh and not is_rsi_extreme:
        label = "TAZE"
        rsi_str = f"{short_rsi:.0f}" if short_rsi is not None else "n/a"
        aciklama = (
            f"Hareket ~{breakout_age} dakika once baslamis, kisa vadeli RSI {rsi_str} -- "
            f"henuz erken asamada gorunuyor."
        )
    elif is_rsi_extreme:
        label = "UZAMIS"
        aciklama = f"Kisa vadeli RSI {short_rsi:.0f} ({rsi_extreme_text}) -- hareketin sonuna yaklasilmis olabilir."
    elif is_fresh:
        label = "TAZE"
        aciklama = f"Hareket ~{breakout_age} dakika once baslamis -- henuz erken asamada."
    else:
        label = "BELIRSIZ"
        rsi_str = f"{short_rsi:.0f}" if short_rsi is not None else "n/a"
        age_str = f"~{breakout_age} dk" if breakout_age is not None else "n/a"
        aciklama = f"Kisa vadeli RSI {rsi_str}, hareket yasi {age_str} -- net bir taze/uzamis ayrimi yok."

    return {
        "label": label,
        "short_rsi": short_rsi,
        "breakout_age_minutes": breakout_age,
        "aciklama": aciklama,
    }


def fetch_klines_generic(symbol: str, interval: str, limit: int):
    try:
        url = f"{BYBIT_BASE_URL}/v5/market/kline"
        r = requests.get(
            url, params={"category": "linear", "symbol": symbol, "interval": interval, "limit": limit},
            timeout=10,
        )
        data = r.json()
        if data.get("retCode") != 0:
            return None
        raw = data.get("result", {}).get("list", [])
        if len(raw) < 3:
            return None
        raw = list(reversed(raw))
        bars = [
            {"open": float(c[1]), "high": float(c[2]), "low": float(c[3]),
             "close": float(c[4]), "turnover": float(c[6])}
            for c in raw
        ]
        return bars
    except Exception as e:
        logging.error(f"{symbol} genel mum verisi hatasi ({interval}): {e}")
        return None


def detect_range_breakout(symbol: str):
    bars = fetch_klines_generic(symbol, interval="60", limit=BREAKOUT_LOOKBACK_HOURS + 2)
    if bars is None or len(bars) < BREAKOUT_LOOKBACK_HOURS + 1:
        logging.info(f"[Kirilim Teshis] {symbol}: yeterli 1 saatlik mum verisi yok, atlaniyor.")
        return None

    breakout_bar = bars[-1]
    range_bars = bars[-(BREAKOUT_LOOKBACK_HOURS + 1):-1]

    range_high = max(b["high"] for b in range_bars)
    range_low = min(b["low"] for b in range_bars)
    if range_low <= 0:
        return None

    range_width_pct = (range_high - range_low) / range_low * 100.0
    if range_width_pct > BREAKOUT_RANGE_MAX_PCT:
        logging.info(
            f"[Kirilim Teshis] {symbol}: aralik cok genis (%{range_width_pct:.1f}, "
            f"limit %{BREAKOUT_RANGE_MAX_PCT}) -- konsolidasyon sayilmadi."
        )
        return None

    range_avg_turnover = sum(b["turnover"] for b in range_bars) / len(range_bars)
    if range_avg_turnover <= 0:
        return None
    volume_ratio = breakout_bar["turnover"] / range_avg_turnover

    direction = None
    if breakout_bar["close"] > range_high and volume_ratio >= BREAKOUT_VOLUME_MULTIPLIER:
        direction = "UP"
    elif breakout_bar["close"] < range_low and volume_ratio >= BREAKOUT_VOLUME_MULTIPLIER:
        direction = "DOWN"

    if direction is None:
        if breakout_bar["close"] > range_high or breakout_bar["close"] < range_low:
            logging.info(
                f"[Kirilim Teshis] {symbol}: fiyat araligi kirmis (dar araligi: %{range_width_pct:.1f}) "
                f"AMA hacim yetersiz ({volume_ratio:.2f}x, gereken {BREAKOUT_VOLUME_MULTIPLIER}x) -- reddedildi."
            )
        else:
            logging.info(
                f"[Kirilim Teshis] {symbol}: dar aralikta (%{range_width_pct:.1f}) ama henuz kirilim yok "
                f"(kapanis {breakout_bar['close']:.6g}, aralik {range_low:.6g}-{range_high:.6g})."
            )
        return None

    return {
        "direction": direction,
        "breakout_close": breakout_bar["close"],
        "range_high": range_high,
        "range_low": range_low,
        "volume_ratio": volume_ratio,
    }


def confirm_breakout_hold(symbol: str, direction: str, breakout_level: float):
    bars = fetch_klines_generic(symbol, interval="5", limit=6)
    if bars is None or len(bars) < 3:
        return None
    recent_closes = [b["close"] for b in bars[-3:]]
    if direction == "UP":
        return all(c >= breakout_level * 0.995 for c in recent_closes)
    else:
        return all(c <= breakout_level * 1.005 for c in recent_closes)


def is_recently_notified_breakout(inst_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cutoff_time = (
        datetime.now(timezone.utc) - timedelta(hours=BREAKOUT_ALERT_COOLDOWN_HOURS)
    ).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) FROM breakout_alerts WHERE inst_id = ? AND timestamp >= ?",
        (inst_id, cutoff_time),
    ).fetchone()
    conn.close()
    return row[0] > 0


def record_breakout_alert(inst_id: str, direction: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO breakout_alerts (timestamp, inst_id, direction) VALUES (?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), inst_id, direction),
    )
    conn.commit()
    conn.close()


def send_breakout_alert(inst_id: str, breakout_info: dict, hold_confirmed):
    direction = breakout_info["direction"]
    emoji = "📈" if direction == "UP" else "📉"
    yon_text = "YUKARI KIRILIM" if direction == "UP" else "ASAGI KIRILIM (SHORT)"

    hold_text = ""
    if hold_confirmed is True:
        hold_text = "✅ 5dk teyit: seviye korunuyor (sahte kirilim degil gibi gorunuyor)"
    elif hold_confirmed is False:
        hold_text = "⚠️ 5dk teyit: fiyat geri donmus olabilir -- dikkatli ol, sahte kirilim (fake breakout) riski var"
    else:
        hold_text = "ℹ️ 5dk teyit verisi yetersiz"

    msg = (
        f"{emoji} *{yon_text}* -- {inst_id}\n\n"
        f"Konsolidasyon araligi: {breakout_info['range_low']:.6g} - {breakout_info['range_high']:.6g}\n"
        f"Kirilim kapanisi: {breakout_info['breakout_close']:.6g}\n"
        f"Kirilim hacmi: pencere ortalamasinin {breakout_info['volume_ratio']:.1f} kati\n"
        f"{hold_text}\n\n"
        f"_Bu ayri bir tespit turudur -- ana hacim/skor sistemi ile bagimsiz calisir. "
        f"Yatirim tavsiyesi degildir._"
    )
    send_telegram_alert(msg)
    record_breakout_alert(inst_id, direction)
    logging.info(f"[Kirilim] {inst_id} icin {direction} kirilim sinyali gonderildi.")


def is_exhausted_reversal_zone(change_24h_pct, freshness_info, cvd_ratio):
    """
    YENI (Madde 1): RSI asiri + hareket ESKI + CVD uyumsuzlugu AYNI ANDA
    varsa, bu artik "giris" degil "cikis/ters donus" bolgesi sayilir.
    Kararlastirildigi gibi boyle bir durumda ana HYPE sinyali TAMAMEN
    BASTIRILIR (hem skor yolu hem buyuk hareket yolu icin) -- ayri bir
    SHORT sinyali degil, sadece sessizce atlanir (zaten kirilim sistemi
    ayri calisiyor).

    CVD uyumsuzlugu: fiyat yukselirken CVD zayifsa (satis baskisi var),
    ya da fiyat duserken CVD guclu alis gosteriyorsa (satis tukenmis
    olabilir) -- ikisi de "yon ile hacim arasinda catisma var" demek.
    """
    if freshness_info is None:
        return False
    if freshness_info.get("label") != "UZAMIS":
        return False
    if cvd_ratio is None:
        return False
    yon_yukari = change_24h_pct >= 0
    if yon_yukari:
        return cvd_ratio <= CVD_SELL_STRONG
    else:
        return cvd_ratio >= CVD_BUY_STRONG


def is_recently_notified_funding(inst_id: str) -> bool:
    """YENI (Madde 6): Funding asiriligi sinyali icin AYRI bir cooldown."""
    conn = sqlite3.connect(DB_PATH)
    cutoff_time = (
        datetime.now(timezone.utc) - timedelta(hours=FUNDING_SCAN_COOLDOWN_HOURS)
    ).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) FROM funding_alerts WHERE inst_id = ? AND timestamp >= ?",
        (inst_id, cutoff_time),
    ).fetchone()
    conn.close()
    return row[0] > 0


def record_funding_alert(inst_id: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO funding_alerts (timestamp, inst_id) VALUES (?, ?)",
        (datetime.now(timezone.utc).isoformat(), inst_id),
    )
    conn.commit()
    conn.close()


def send_funding_alert(inst_id: str, funding_pct: float, change_24h_pct: float):
    """
    YENI (Madde 6): Funding asiriligi bazli, ana hype sisteminden TAMAMEN
    BAGIMSIZ, paralel bir tarama. "Funding cok yuksek/dusuk VE fiyat zaten
    hareket etmis" durumunu yakalar -- coin hic hype sinyali vermese bile.
    """
    if funding_pct >= FUNDING_SCAN_EXTREME_PCT and change_24h_pct > 0:
        emoji = "⚠️"
        aciklama = (
            f"Funding %{funding_pct:.3f} (asiri pozitif) + fiyat zaten %{change_24h_pct:.1f} "
            f"yukselmis -- long tarafi kalabalik, ani bir long tasfiyesi (fiyat dususu) "
            f"riski artmis olabilir."
        )
    elif funding_pct <= FUNDING_SCAN_EXTREME_PCT * -1 and change_24h_pct < 0:
        emoji = "⚠️"
        aciklama = (
            f"Funding %{funding_pct:.3f} (asiri negatif) + fiyat zaten %{change_24h_pct:.1f} "
            f"dusmus -- short tarafi kalabalik, ani bir short squeeze (fiyat sicramasi) "
            f"riski artmis olabilir."
        )
    else:
        return  # kombinasyon net degil, gonderme

    msg = (
        f"{emoji} *FUNDING ASIRILIGI* -- {inst_id}\n\n"
        f"{aciklama}\n\n"
        f"_Bu, ana hype sisteminden bagimsiz, sadece funding/fiyat kombinasyonuna "
        f"dayanan ayri bir tarama. Yatirim tavsiyesi degildir._"
    )
    send_telegram_alert(msg)
    record_funding_alert(inst_id)
    logging.info(f"[Funding] {inst_id} icin funding asiriligi sinyali gonderildi.")


def get_cvd_buy_ratio(inst_id: str, limit: int = 300):
    url = f"{BYBIT_BASE_URL}/v5/market/recent-trade"
    try:
        response = requests.get(
            url, params={"category": "linear", "symbol": inst_id, "limit": limit}, timeout=10
        )
        data = response.json()
        if data.get("retCode") != 0:
            return None
        trades = data.get("result", {}).get("list", [])
        if not trades:
            return None
        buy_vol = sum(float(t["size"]) for t in trades if t.get("side") == "Buy")
        sell_vol = sum(float(t["size"]) for t in trades if t.get("side") == "Sell")
        total = buy_vol + sell_vol
        if total == 0:
            return None
        return buy_vol / total
    except Exception as e:
        logging.error(f"{inst_id} CVD hesabi hatasi: {e}")
        return None


def compute_multi_timeframe_technicals(inst_id: str):
    """
    YENI: 15dk / 1sa / 4sa mumlarla RSI ve MACD hesaplar -- bilgilendirici
    amaclidir, giris kapisini ETKILEMEZ, sadece kapiyi gecen sinyalin
    mesajina eklenen detayli bir teknik ozet saglar.

    Donus: {'15dk': {'rsi':.., 'macd_hist':..}, '1sa': {...}, '4sa': {...}}
    Herhangi bir zaman dilimi icin veri yetersizse o dilimin degerleri
    None kalir (hata firlatmaz).
    """
    result = {}
    for tf_key, interval, limit in [("15dk", "15", 100), ("1sa", "60", 100), ("4sa", "240", 100)]:
        bars = fetch_klines_generic(inst_id, interval=interval, limit=limit)
        if bars is None or len(bars) < 35:
            result[tf_key] = {"rsi": None, "macd_hist": None}
            continue
        closes = [b["close"] for b in bars]
        rsi = calculate_rsi(closes, period=14)
        macd = calculate_macd(closes)
        result[tf_key] = {
            "rsi": rsi,
            "macd_hist": macd["histogram"] if macd else None,
        }
    return result


def get_spot_cvd_buy_ratio(inst_id: str, limit: int = 300):
    """
    YENI (Madde 5): Ayni hesaplamayi SPOT piyasa icin yapar (category='spot').
    Futures CVD ile kiyaslanip "bu yukselis gercek/spot alicilardan mi,
    yoksa sadece kaldiracli/futures kumarindan mi geliyor" ayrimini yapmak
    icin kullanilir.

    ONEMLI: Bybit'te her futures sembolunun bir spot karsiligi OLMAYABILIR
    (bircok kucuk/yeni coin sadece vadeli islemde listelenir). Bu durumda
    Bybit hata donuyor, biz de None donup sessizce atliyoruz -- hata
    firlatmiyoruz, sistemi cokertmiyor.
    """
    url = f"{BYBIT_BASE_URL}/v5/market/recent-trade"
    try:
        response = requests.get(
            url, params={"category": "spot", "symbol": inst_id, "limit": limit}, timeout=10
        )
        data = response.json()
        if data.get("retCode") != 0:
            return None  # bu sembol icin spot piyasa yok olabilir, normal
        trades = data.get("result", {}).get("list", [])
        if not trades:
            return None
        buy_vol = sum(float(t["size"]) for t in trades if t.get("side") == "Buy")
        sell_vol = sum(float(t["size"]) for t in trades if t.get("side") == "Sell")
        total = buy_vol + sell_vol
        if total == 0:
            return None
        return buy_vol / total
    except Exception as e:
        logging.error(f"{inst_id} Spot CVD hesabi hatasi: {e}")
        return None


def calculate_freshness_ratio(inst_id: str) -> float:
    try:
        url_60d = f"{BYBIT_BASE_URL}/v5/market/kline"
        res_60d = requests.get(
            url_60d, params={"category": "linear", "symbol": inst_id, "interval": "D", "limit": 60},
            timeout=10,
        ).json()
        data_60d = res_60d.get("result", {}).get("list", [])

        if len(data_60d) < 30:
            return 1.0

        total_60d_vol = sum([float(c[6]) for c in data_60d])
        avg_hourly_vol_60d = (total_60d_vol / len(data_60d)) / 24.0

        url_2h = f"{BYBIT_BASE_URL}/v5/market/kline"
        res_2h = requests.get(
            url_2h, params={"category": "linear", "symbol": inst_id, "interval": "60", "limit": 2},
            timeout=10,
        ).json()
        data_2h = res_2h.get("result", {}).get("list", [])

        if len(data_2h) < 2:
            return 1.0

        total_2h_vol = sum([float(c[6]) for c in data_2h])
        avg_hourly_vol_recent = total_2h_vol / 2.0

        if avg_hourly_vol_60d == 0:
            return 1.0

        ratio = avg_hourly_vol_recent / avg_hourly_vol_60d
        return round(ratio, 2)

    except Exception as e:
        logging.error(f"{inst_id} 60 günlük hacim hesabı hatası: {e}")
        return 1.0


def generate_commentary(change_pct, oi_change_pct, cvd_ratio, position_in_range, freshness_ratio,
                         funding_pct=None, funding_change_pct=None, spot_cvd_ratio=None,
                         freshness_label=None, short_rsi=None):
    notlar = []
    yon_yukari = change_pct >= 0

    exhausted = (
        position_in_range is not None
        and position_in_range >= EXHAUSTION_POSITION_THRESHOLD
        and freshness_ratio is not None
        and freshness_ratio < 1.0
    )
    if exhausted:
        notlar.append(
            "Fiyat zaten 24s bandinin zirvesine cok yakin ve hacim ivmesi yavasliyor "
            "-- tukenme/geri cekilme riski yuksek."
        )

    if oi_change_pct is not None:
        if yon_yukari and oi_change_pct >= OI_CHANGE_STRONG_PCT:
            notlar.append(
                f"Open Interest 24s'te %{oi_change_pct:.1f} artti -- yeni pozisyon girisi "
                f"fiyat yukselisini teyit ediyor."
            )
        elif yon_yukari and oi_change_pct <= OI_CHANGE_WEAK_NEGATIVE_PCT:
            notlar.append(
                "Fiyat yukseliyor ama Open Interest dusuyor -- bu yukselis muhtemelen kisa "
                "pozisyonlarin kapanmasindan (short squeeze) kaynaklaniyor, yeni para girisi "
                "zayif, hareket kirilgan olabilir."
            )
        elif (not yon_yukari) and oi_change_pct >= OI_CHANGE_STRONG_PCT:
            notlar.append(
                f"Fiyat duserken Open Interest %{oi_change_pct:.1f} artmis -- yeni kisa (short) "
                f"pozisyon girisi olabilir, dusus devam edebilir."
            )

    if cvd_ratio is not None:
        if yon_yukari and cvd_ratio >= CVD_BUY_STRONG:
            notlar.append(
                f"Alici baskili hacim var (son islemlerin %{cvd_ratio*100:.0f}'i alis) -- "
                f"fiyat yukselmeye devam edebilir."
            )
        elif yon_yukari and cvd_ratio <= CVD_SELL_STRONG:
            notlar.append(
                f"Fiyat yukselirken son islemlerde satis baskisi daha agir (alis orani %"
                f"{cvd_ratio*100:.0f}) -- bu uyumsuzluk dikkat gerektirir, saglıksiz/"
                f"manipulatif olabilir."
            )
        elif (not yon_yukari) and cvd_ratio <= CVD_SELL_STRONG:
            notlar.append(
                f"Satici baskili hacim var (alis orani sadece %{cvd_ratio*100:.0f}) -- "
                f"dusus baskisi guclu gorunuyor."
            )

    # YENI (Madde 3): Funding rate + OI + fiyat kombinasyonu.
    # Kural: Fiyat yukselirken + OI artarken + Funding NEGATIF -> mukemmel long
    # (short squeeze potansiyeli). Fiyat yukselirken + OI artarken + Funding
    # ASIRI POZITIF -> riskli (tepede long birikmis, patlayabilir).
    if funding_pct is not None and oi_change_pct is not None:
        oi_artiyor = oi_change_pct >= OI_CHANGE_STRONG_PCT
        if yon_yukari and oi_artiyor and funding_pct <= FUNDING_EXTREME_NEG_PCT:
            notlar.append(
                f"Funding NEGATIF (%{funding_pct:.3f}) + OI artisi + fiyat yukselisi -- "
                f"MUKEMMEL LONG kurulumu olabilir, short squeeze potansiyeli var."
            )
        elif yon_yukari and oi_artiyor and funding_pct >= FUNDING_EXTREME_POS_PCT:
            notlar.append(
                f"Funding ASIRI POZITIF (%{funding_pct:.3f}) -- tepede long birikmis, "
                f"ani bir dususe (long tasfiyesi) karsi riskli olabilir."
            )

    if funding_change_pct is not None and abs(funding_change_pct) >= 0.02:
        yon_text = "artmis" if funding_change_pct > 0 else "azalmis"
        notlar.append(
            f"Funding rate son ~{FUNDING_HISTORY_LOOKBACK_HOURS:.0f} saatte %{abs(funding_change_pct):.3f} {yon_text}."
        )

    # YENI (Madde 5): Spot CVD vs Futures CVD ayrimi.
    if spot_cvd_ratio is not None and cvd_ratio is not None and yon_yukari:
        if cvd_ratio >= CVD_BUY_STRONG and spot_cvd_ratio < 0.50:
            notlar.append(
                f"Futures CVD guclu (%{cvd_ratio*100:.0f}) ama Spot CVD zayif (%{spot_cvd_ratio*100:.0f}) "
                f"-- yukselis agirlikli olarak kaldiracli (futures) pozisyonlardan geliyor, "
                f"omru kisa olabilir."
            )
        elif cvd_ratio >= 0.55 and spot_cvd_ratio >= 0.55:
            notlar.append(
                f"Hem Spot (%{spot_cvd_ratio*100:.0f}) hem Futures (%{cvd_ratio*100:.0f}) CVD guclu "
                f"-- gercek/kurumsal alim olabilir, daha saglikli bir yukselis."
            )

    # YENI (Madde 4): "Taze + RSI<60 + CVD>65" -> ozel vurgu (Guclu Setup).
    if (
        freshness_label == "TAZE"
        and short_rsi is not None and short_rsi < STRONG_SETUP_RSI_MAX
        and cvd_ratio is not None and cvd_ratio > STRONG_SETUP_CVD_MIN
    ):
        notlar.append(
            "🔥 GUCLU SETUP: Taze + RSI<60 + CVD>65 -- agresif akumulasyon, "
            "saglikli bir yukselis baslangici olabilir."
        )

    if not notlar:
        notlar.append("Ek teyit sinyalleri (OI/CVD) notr veya yetersiz veri -- sadece "
                       "hacim/fiyat verisine dayanan bir sinyal.")

    return " ".join(notlar)


def run_scanner():
    """Ana piyasa tarama ve hesaplama döngüsü."""
    logging.info("🔍 Bybit Hype taraması başlatılıyor...")
    logging.info(f"[Ayar Kontrolu] TELEGRAM_BOT_TOKEN tanimli mi: {bool(TELEGRAM_BOT_TOKEN)}")
    logging.info(f"[Ayar Kontrolu] TELEGRAM_CHAT_ID tanimli mi: {bool(TELEGRAM_CHAT_ID)}")

    tickers = get_bybit_tickers()
    if not tickers:
        logging.warning("Bybit'ten ticker verisi alınamadı.")
        return

    oi_map = get_all_open_interest(tickers)
    logging.info(f"[OI] {len(oi_map)} enstruman icin Open Interest verisi alindi.")

    try:
        trending_symbols = get_coingecko_trending()
        logging.info(f"[CoinGecko] Trending listesi: {len(trending_symbols)} sembol.")
    except Exception as e:
        logging.error(f"[CoinGecko] Trending listesi alinamadi: {e}")
        trending_symbols = set()

    alerts_to_send = []
    all_results = []

    for t in tickers:
        try:
            inst_id = t.get("symbol", "")
            if not inst_id.endswith("USDT"):
                continue

            last_price = float(t.get("lastPrice", 0))
            open_24h = float(t.get("prevPrice24h", last_price))
            high_24h = float(t.get("highPrice24h", last_price))
            low_24h = float(t.get("lowPrice24h", last_price))

            if open_24h == 0:
                continue

            change_24h_pct = ((last_price - open_24h) / open_24h) * 100.0
            turnover_24h = float(t.get("turnover24h", 0))

            if turnover_24h < MIN_TURNOVER_24H:
                continue

            avg_turnover = get_average_turnover(inst_id)
            if avg_turnover is not None and avg_turnover > 0:
                volume_ratio = turnover_24h / avg_turnover
                power_score = volume_ratio * (1 + abs(change_24h_pct) / 100.0)
            else:
                volume_ratio = None
                power_score = None

            freshness_ratio = 1.0
            if power_score is not None and power_score > FRESHNESS_CHECK_VOLUME_RATIO:
                freshness_ratio = calculate_freshness_ratio(inst_id)

            final_score = power_score * freshness_ratio if power_score is not None else None

            current_oi = oi_map.get(inst_id)
            oi_change_pct = None
            if current_oi is not None:
                prev_oi = get_previous_oi(inst_id)
                if prev_oi and prev_oi > 0:
                    oi_change_pct = (current_oi - prev_oi) / prev_oi * 100.0

            # YENI (Madde 3, 6): funding rate ticker cevabinin ICINDE zaten
            # geliyor -- ekstra istek gerekmiyor.
            try:
                funding_rate = float(t.get("fundingRate")) if t.get("fundingRate") not in (None, "") else None
            except (TypeError, ValueError):
                funding_rate = None
            funding_pct = funding_rate * 100 if funding_rate is not None else None

            position_in_range = None
            if high_24h > low_24h:
                position_in_range = (last_price - low_24h) / (high_24h - low_24h)

            obs_data = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "inst_id": inst_id,
                "last_price": last_price,
                "change_24h_pct": change_24h_pct,
                "turnover_24h": turnover_24h,
                "power_score": power_score,
                "freshness_ratio": freshness_ratio,
                "final_score": final_score,
                "notified": 0,
                "oi_value": current_oi,
                "oi_change_pct": oi_change_pct,
                "cvd_buy_ratio": None,
                "position_in_range": position_in_range,
                "yorum": None,
                "volume_ratio": volume_ratio,
                "funding_rate": funding_rate,
            }

            all_results.append(obs_data)

            # YENI (Madde 6): Funding asiriligi bazli, ana hype sisteminden
            # BAGIMSIZ, paralel bir tarama. Ekstra API cagrisi gerektirmiyor
            # (funding_rate zaten ticker'dan geldi).
            try:
                if funding_pct is not None and not is_recently_notified_funding(inst_id):
                    is_funding_extreme = (
                        funding_pct >= FUNDING_SCAN_EXTREME_PCT or funding_pct <= -FUNDING_SCAN_EXTREME_PCT
                    )
                    price_moved_enough = abs(change_24h_pct) >= FUNDING_SCAN_MIN_PRICE_MOVE_PCT
                    if is_funding_extreme and price_moved_enough:
                        send_funding_alert(inst_id, funding_pct, change_24h_pct)
            except Exception as e:
                logging.error(f"{inst_id} funding taramasi hatasi: {e}")

            try:
                volume_prefilter_passed = (
                    volume_ratio is not None and volume_ratio >= BREAKOUT_PREFILTER_VOLUME_RATIO
                )
                price_prefilter_passed = abs(change_24h_pct) >= BREAKOUT_PREFILTER_PRICE_CHANGE_PCT

                if (
                    (volume_prefilter_passed or price_prefilter_passed)
                    and not is_recently_notified_breakout(inst_id)
                ):
                    breakout_info = detect_range_breakout(inst_id)
                    if breakout_info is not None:
                        hold_confirmed = confirm_breakout_hold(
                            inst_id, breakout_info["direction"], breakout_info["breakout_close"]
                        )
                        send_breakout_alert(inst_id, breakout_info, hold_confirmed)
            except Exception as e:
                logging.error(f"{inst_id} kirilim tespiti hatasi: {e}")

            score_path_passed = (
                final_score is not None and final_score >= ALERT_POWER_SCORE_THRESHOLD
            )
            big_move_path_passed = abs(change_24h_pct) >= BIG_MOVE_PRICE_CHANGE_PCT

            # NOT: score_path/big_move_path artik SADECE ucuz bir ON-FILTRE --
            # hangi adaylar icin pahali (CVD, coklu zaman dilimi vb.) hesaplama
            # yapilacagini belirliyor. Asil GONDERIM KARARI, asagidaki YENI
            # ZORUNLU KAPI tarafindan veriliyor.
            should_notify_preliminary = (
                (score_path_passed or big_move_path_passed)
                and not is_recently_notified(inst_id)
                and change_24h_pct >= 0  # YENI: sadece YUKSELIS yonu (Burakcan'in RSI<60+CVD>65 kurali long icin)
            )

            if should_notify_preliminary:
                cvd_ratio = get_cvd_buy_ratio(inst_id)
                obs_data["cvd_buy_ratio"] = cvd_ratio

                try:
                    freshness_info = classify_freshness(inst_id)
                except Exception as e:
                    logging.error(f"{inst_id} TAZE/UZAMIS tespiti hatasi: {e}")
                    freshness_info = {"label": "BELIRSIZ", "short_rsi": None,
                                       "breakout_age_minutes": None, "aciklama": "Hesaplanamadi."}

                short_rsi = freshness_info.get("short_rsi")

                # --- YENI ZORUNLU GIRIS KAPISI ---
                # Burakcan'in notuna gore: RSI < 60 (henuz asiri isinmamis)
                # VE CVD > 65 (guclu, net alis baskisi) ZORUNLU. Ayrica
                # tukenme/ters donus bolgesinde DEGIL VE OI, scalp ya da
                # swing olceginde teyit ediyor olmali.
                rsi_gate_passed = short_rsi is not None and short_rsi < ENTRY_GATE_RSI_MAX
                cvd_gate_passed = cvd_ratio is not None and cvd_ratio > ENTRY_GATE_CVD_MIN
                not_exhausted = not is_exhausted_reversal_zone(change_24h_pct, freshness_info, cvd_ratio)
                oi_multiframe = check_oi_multiframe_confirmation(inst_id, current_oi)

                gate_passed = rsi_gate_passed and cvd_gate_passed and not_exhausted and oi_multiframe["passed"]

                if not gate_passed:
                    reasons = []
                    if not rsi_gate_passed:
                        reasons.append(f"RSI kapisi gecmedi ({short_rsi})")
                    if not cvd_gate_passed:
                        reasons.append(f"CVD kapisi gecmedi ({cvd_ratio})")
                    if not not_exhausted:
                        reasons.append("tukenme/ters donus bolgesinde")
                    if not oi_multiframe["passed"]:
                        reasons.append(f"OI teyidi yok ({oi_multiframe['detail']})")
                    logging.info(f"[Kapi Reddi] {inst_id}: {' | '.join(reasons)}")
                else:
                    # YENI: funding degisimi (son ~6 saat) -- bilgilendirici.
                    prev_funding = get_previous_funding_rate(inst_id)
                    funding_change_pct = None
                    if prev_funding is not None and funding_pct is not None:
                        funding_change_pct = funding_pct - (prev_funding * 100)

                    try:
                        spot_cvd_ratio = get_spot_cvd_buy_ratio(inst_id)
                    except Exception as e:
                        logging.error(f"{inst_id} Spot CVD tespiti hatasi: {e}")
                        spot_cvd_ratio = None

                    try:
                        mtf = compute_multi_timeframe_technicals(inst_id)
                    except Exception as e:
                        logging.error(f"{inst_id} coklu zaman dilimi analizi hatasi: {e}")
                        mtf = None

                    yorum = generate_commentary(
                        change_24h_pct, oi_change_pct, cvd_ratio, position_in_range, freshness_ratio,
                        funding_pct=funding_pct, funding_change_pct=funding_change_pct,
                        spot_cvd_ratio=spot_cvd_ratio, freshness_label=freshness_info.get("label"),
                        short_rsi=short_rsi,
                    )

                    base_symbol = inst_id.replace("USDT", "").strip().upper()
                    is_trending = base_symbol in trending_symbols
                    if is_trending:
                        yorum += (" 🔥 Ayrica CoinGecko'nun Trending (en cok aranan) listesinde de var -- "
                                  "hem hacim hem genel arama ilgisi ayni anda yukseliyor, capraz teyit guclu.")

                    obs_data["yorum"] = yorum
                    obs_data["notified"] = 1

                    alerts_to_send.append({
                        "inst_id": inst_id,
                        "price": last_price,
                        "change": change_24h_pct,
                        "turnover": turnover_24h,
                        "freshness_info": freshness_info,
                        "score": final_score,
                        "volume_ratio": volume_ratio,
                        "freshness": freshness_ratio,
                        "oi_change_pct": oi_change_pct,
                        "oi_multiframe": oi_multiframe,
                        "cvd_ratio": cvd_ratio,
                        "spot_cvd_ratio": spot_cvd_ratio,
                        "funding_pct": funding_pct,
                        "mtf": mtf,
                        "yorum": yorum,
                    })

            record_observation(obs_data)

        except Exception as e:
            logging.error(f"Hata ({t.get('symbol')}): {e}")

    if alerts_to_send:
        msg = "🎯 *GIRIS FIRSATI TESPIT EDILDI!*\n_(RSI<60 + CVD>65 + OI teyidi -- kapiyi gecen sinyaller)_\n\n"
        for a in alerts_to_send:
            msg += f"🟢 *{a['inst_id']}*\n"

            fi = a.get("freshness_info", {})
            label = fi.get("label", "BELIRSIZ")
            if label == "TAZE":
                msg += "🟢 *TAZE* -- erken asamada, potansiyel devam edebilir\n"
            elif label == "UZAMIS":
                msg += "🟡 *UZAMIS ama CVD teyit ediyor* -- dikkatli takip et\n"
            else:
                msg += "⚪ *BELIRSIZ* -- taze/uzamis ayrimi icin yeterli veri yok\n"
            if fi.get("aciklama"):
                msg += f"   _{fi['aciklama']}_\n"

            msg += f"• Fiyat: `{a['price']}`\n"
            msg += f"• 24s Değişim: `%{a['change']:.2f}`\n"
            msg += f"• 24s Ciro: `{a['turnover']:,.0f} USDT`\n"
            vr_str = f"{a['volume_ratio']:.2f}x" if a["volume_ratio"] is not None else "n/a (yeterli gecmis yok)"
            msg += f"• Hacim Orani (normale gore): `{vr_str}`\n"
            msg += f"• Hacim İvmesi (kisa vade): `{a['freshness']:.2f}x`\n"

            # YENI: OI coklu zaman dilimi detayi (scalp/swing).
            oi_mf = a.get("oi_multiframe")
            if oi_mf and oi_mf.get("detail"):
                scale_text = {"scalp": "SCALP olcek", "swing": "SWING olcek"}.get(oi_mf.get("scale"), "")
                msg += f"• OI Teyidi ({scale_text}): `{oi_mf['detail']}`\n"
            if a["oi_change_pct"] is not None:
                msg += f"• OI Değişimi (24s, referans): `%{a['oi_change_pct']:.1f}`\n"

            if a["cvd_ratio"] is not None:
                msg += f"• Futures CVD: `%{a['cvd_ratio']*100:.0f}`\n"
            if a.get("spot_cvd_ratio") is not None:
                msg += f"• Spot CVD: `%{a['spot_cvd_ratio']*100:.0f}`\n"
            if a.get("funding_pct") is not None:
                msg += f"• Funding: `%{a['funding_pct']:.3f}`\n"

            # YENI: Coklu zaman dilimi RSI/MACD ozeti (15dk/1sa/4sa).
            mtf = a.get("mtf")
            if mtf:
                parts = []
                for tf_key in ["15dk", "1sa", "4sa"]:
                    tf = mtf.get(tf_key, {})
                    rsi_v = tf.get("rsi")
                    macd_v = tf.get("macd_hist")
                    rsi_str = f"{rsi_v:.0f}" if rsi_v is not None else "n/a"
                    macd_str = ("+" if (macd_v is not None and macd_v > 0) else
                                "-" if (macd_v is not None and macd_v < 0) else "n/a")
                    parts.append(f"{tf_key} RSI {rsi_str}/MACD{macd_str}")
                msg += f"• Coklu Zaman Dilimi: `{' | '.join(parts)}`\n"

            score_str = f"{a['score']:.1f}" if a["score"] is not None else "n/a (BUYUK HAREKET yolu ile geldi)"
            msg += f"• *Final Skor:* `{score_str}`\n"
            msg += f"📝 _{a['yorum']}_\n\n"

        send_telegram_alert(msg)

    top_candidates = sorted(
        all_results, key=lambda x: x["final_score"] if x["final_score"] is not None else -1, reverse=True
    )[:5]
    if top_candidates:
        logging.info("📊 Şu Anki En Yüksek Skorlu 5 Coin:")
        for c in top_candidates:
            oi_str = f"{c['oi_change_pct']:.1f}%" if c.get("oi_change_pct") is not None else "n/a"
            score_str = f"{c['final_score']:.2f}" if c["final_score"] is not None else "n/a (yeterli gecmis yok)"
            vr_str = f"{c['volume_ratio']:.2f}x" if c.get("volume_ratio") is not None else "n/a"
            logging.info(
                f"   -> {c['inst_id']:<18} | Değişim: %{c['change_24h_pct']:<6.2f} | "
                f"Hacim Orani: {vr_str:<8} | OI: {oi_str:<8} | Skor: {score_str}"
            )

    logging.info("✅ Tarama tamamlandı.")


def generate_4h_report():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    since_ts = (datetime.now(timezone.utc) - timedelta(hours=4, minutes=15)).isoformat()
    cursor.execute(
        """
        SELECT inst_id, timestamp, last_price, notified FROM hype_observations
        WHERE timestamp >= ? ORDER BY inst_id, timestamp ASC
        """,
        (since_ts,),
    )
    rows = cursor.fetchall()
    conn.close()

    by_symbol = {}
    for inst_id, ts, price, notified in rows:
        if not price:
            continue
        if inst_id not in by_symbol:
            by_symbol[inst_id] = {"first_price": price, "last_price": price, "was_notified": bool(notified)}
        else:
            by_symbol[inst_id]["last_price"] = price
            if notified:
                by_symbol[inst_id]["was_notified"] = True

    movers = []
    for symbol, d in by_symbol.items():
        if d["first_price"] and d["first_price"] > 0:
            pct = (d["last_price"] - d["first_price"]) / d["first_price"] * 100.0
            if abs(pct) >= REPORT_MIN_MOVE_PCT:
                movers.append({"symbol": symbol, "pct": pct, "caught": d["was_notified"]})

    movers.sort(key=lambda x: -abs(x["pct"]))
    return movers


def send_4h_report():
    movers = generate_4h_report()
    total = len(movers)
    caught = sum(1 for m in movers if m["caught"])

    msg = "📊 *4 SAATLIK OZET RAPORU*\n\n"
    if total == 0:
        msg += f"Son 4 saatte %{REPORT_MIN_MOVE_PCT:.0f}+ hareket eden coin olmadi (ya da yeterli veri yok)."
    else:
        msg += f"Son 4 saatte %{REPORT_MIN_MOVE_PCT:.0f}+ hareket eden coin sayisi: *{total}*\n"
        msg += f"Sistemin yakaladigi (alarm verdigi): *{caught}/{total}*\n\n"
        for m in movers[:REPORT_MAX_ITEMS_SHOWN]:
            tag = "✅" if m["caught"] else "❌"
            direction = "🟢" if m["pct"] >= 0 else "🔴"
            msg += f"{tag}{direction} {m['symbol']}: %{m['pct']:.1f}\n"
        if total > REPORT_MAX_ITEMS_SHOWN:
            msg += f"\n... ve {total - REPORT_MAX_ITEMS_SHOWN} coin daha (mesaj uzunlugu siniri)."

    send_telegram_alert(msg)
    logging.info(f"[Rapor] 4 saatlik ozet gonderildi. {total} hareketli coin, {caught} tanesi yakalanmisti.")


def main_loop():
    init_db()
    last_report_ts = time.time()
    while True:
        try:
            run_scanner()
        except Exception as e:
            logging.error(f"Ana döngüde beklenmeyen hata: {e}")

        now = time.time()
        if now - last_report_ts >= REPORT_INTERVAL_SECONDS:
            try:
                send_4h_report()
            except Exception as e:
                logging.error(f"4 saatlik rapor gonderilirken hata: {e}")
            last_report_ts = now

        time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Bybit Hype Bulucu Bot ve Veritabanı Sorgulayıcı"
    )
    parser.add_argument(
        "--gecmis",
        type=str,
        help="Belirtilen sembolün geçmiş veritabanı kayıtlarını gösterir. Örn: ONDOUSDT",
    )

    args = parser.parse_args()

    if args.gecmis:
        init_db()
        rows = get_symbol_history(args.gecmis)
        print(f"\n📊 {args.gecmis} - Son Geçmiş Kayıtları:")
        print("-" * 100)
        for r in rows:
            ts, price, chg, turnover, power, fresh, final, oi_chg, cvd, yorum, vol_ratio = r
            oi_str = f"%{oi_chg:.1f}" if oi_chg is not None else "n/a"
            cvd_str = f"%{cvd*100:.0f}" if cvd is not None else "n/a"
            final_str = f"{final:.2f}" if final is not None else "n/a"
            vol_str = f"{vol_ratio:.2f}x" if vol_ratio is not None else "n/a"
            print(f"{ts[:19]} | Fiyat:{price:<10.4f} | 24s%:{chg:<7.2f} | Hacim Orani:{vol_str:<8} | Skor:{final_str:<8} "
                  f"| OI:{oi_str:<7} | CVD:{cvd_str:<6}")
            if yorum:
                print(f"    Not: {yorum}")
        print("-" * 100)
    else:
        flask_thread = threading.Thread(target=run_flask, daemon=True)
        flask_thread.start()
        main_loop()

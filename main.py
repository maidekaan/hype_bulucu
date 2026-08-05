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

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

MIN_TURNOVER_24H = 300_000.0

# --- YENI SISTEM: 3 Aylik Gercek Hacim Gecmisi ---
# Bybit'in kendi 1 saatlik mum gecmisinden (sayfalama ile) 3 aylik gercek
# veri cekilip 'volume_history' tablosuna doldurulur. Boylece hacim
# karsilastirmasi GUNLERCE beklemeye gerek kalmadan, ilk taramadan itibaren
# saglam bir 3 aylik baseline uzerinden yapilabilir.
VOLUME_HISTORY_DAYS = 90
VOLUME_HISTORY_EXCLUDE_RECENT_HOURS = 2.0   # son X saat baseline'a dahil edilmez (kendi kendini kirletmesin diye)
VOLUME_HISTORY_MIN_SAMPLES = 200            # bu kadar ornek yoksa baseline guvenilmez sayilir

# --- ALTIN KURAL (kullanicinin kararlastirdigi): RSI<60 VE CVD>65 AYNI ANDA ---
GOLDEN_RULE_RSI_MAX = 60.0
GOLDEN_RULE_CVD_MIN = 0.65

# --- Hacim sicramasi ve tazelik filtresi (kullanicinin sundugu script'ten) ---
VOLUME_SPIKE_MIN_RATIO = 3.0          # son 1sa hacmi, 3 aylik ortalamanin bu katini gecmeli
FRESHNESS_MAX_5H_CHANGE_PCT = 15.0    # son 5 saatte bu yuzdenin uzerinde hareket etmisse 'UZAMIS' sayilir, elenir

# --- 4 Saatlik Trend Onayi ---
TREND_4H_MA_SHORT = 20
TREND_4H_MA_LONG = 50

ALERT_COOLDOWN_HOURS = 6.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# ---------------------------------------------------------------------------
# VERİTABANI İŞLEMLERİ (SQLite)
# ---------------------------------------------------------------------------


def init_db():
    """
    SQLite veritabanini ve gerekli tablolari olusturur. Eski (v17 ve
    oncesi) karmasik tablolara artik ihtiyac yok -- sade, yeni sisteme
    uygun iki tablo: sinyal kayitlari (cooldown icin) ve 3 aylik hacim
    gecmisi.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS hype_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            inst_id TEXT NOT NULL,
            last_price REAL,
            volume_ratio REAL,
            rsi REAL,
            cvd_ratio REAL,
            notified INTEGER DEFAULT 0
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_inst_time ON hype_observations (inst_id, timestamp)"
    )

    # YENI: 3 aylik gercek hacim gecmisi -- hem seed ile bir kerede
    # doldurulur, hem canli tarama sirasinda fethedilen barlarla
    # kendiliginden guncel kalir (INSERT OR IGNORE ile).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS volume_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inst_id TEXT NOT NULL,
            bar_time TEXT NOT NULL,
            turnover REAL NOT NULL,
            UNIQUE(inst_id, bar_time)
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_volhist_lookup ON volume_history (inst_id, bar_time)"
    )

    conn.commit()
    conn.close()


def record_observation(inst_id, last_price, volume_ratio, rsi, cvd_ratio, notified):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO hype_observations
           (timestamp, inst_id, last_price, volume_ratio, rsi, cvd_ratio, notified)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (datetime.now(timezone.utc).isoformat(), inst_id, last_price, volume_ratio, rsi, cvd_ratio, notified),
    )
    conn.commit()
    conn.close()


def is_recently_notified(inst_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=ALERT_COOLDOWN_HOURS)).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) FROM hype_observations WHERE inst_id=? AND notified=1 AND timestamp>=?",
        (inst_id, cutoff),
    ).fetchone()
    conn.close()
    return row[0] > 0


def get_symbol_history(inst_id: str, limit: int = 10):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT timestamp, last_price, volume_ratio, rsi, cvd_ratio, notified
           FROM hype_observations WHERE inst_id=? ORDER BY timestamp DESC LIMIT ?""",
        (inst_id, limit),
    ).fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------------------------


def send_telegram_alert(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("Telegram Token veya Chat ID bulunamadı. Bildirim atlanıyor.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code != 200:
            logging.error(f"Telegram API Hatası: {res.text}")
    except Exception as e:
        logging.error(f"Telegram bildirimi gönderilirken hata oluştu: {e}")


# ---------------------------------------------------------------------------
# BYBIT VERİ ÇEKME
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


def fetch_klines_generic(symbol: str, interval: str, limit: int, end_time_ms=None):
    """
    Bybit'ten herhangi bir zaman dilimi icin mum verisi ceker, KRONOLOJIK
    (en eski once) sirada dondurur. 'end_time_ms' verilirse, o zamandan
    GERIYE dogru veri ceker -- 3 aylik seed'de sayfalama icin kullanilir.
    Donus: [{'open_time_ms','open','high','low','close','turnover'}, ...] ya da None.
    """
    try:
        params = {"category": "linear", "symbol": symbol, "interval": interval, "limit": limit}
        if end_time_ms is not None:
            params["end"] = end_time_ms
        url = f"{BYBIT_BASE_URL}/v5/market/kline"
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        if data.get("retCode") != 0:
            return None
        raw = data.get("result", {}).get("list", [])
        if not raw:
            return None
        raw = list(reversed(raw))
        bars = [
            {"open_time_ms": int(c[0]), "open": float(c[1]), "high": float(c[2]),
             "low": float(c[3]), "close": float(c[4]), "turnover": float(c[6])}
            for c in raw
        ]
        return bars
    except Exception as e:
        logging.error(f"{symbol} mum verisi hatasi ({interval}): {e}")
        return None


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


# ---------------------------------------------------------------------------
# YENI: 3 AYLIK HACIM GECMISI -- SEED + BASELINE SORGUSU
# ---------------------------------------------------------------------------

_volume_seed_status = {"running": False, "done": 0, "total": 0, "started_at": None, "finished_at": None}


def store_volume_bars(inst_id: str, bars):
    """Cekilen 1sa mumlarini volume_history'e yazar (UNIQUE ile tekrar eklemeyi onler)."""
    if not bars:
        return
    conn = sqlite3.connect(DB_PATH)
    for b in bars:
        bar_time = datetime.fromtimestamp(b["open_time_ms"] / 1000.0, tz=timezone.utc).isoformat()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO volume_history (inst_id, bar_time, turnover) VALUES (?, ?, ?)",
                (inst_id, bar_time, b["turnover"]),
            )
        except Exception:
            continue
    conn.commit()
    conn.close()


def _fetch_full_volume_history_for_symbol(inst_id: str, days: int = VOLUME_HISTORY_DAYS):
    """
    Bir coin icin GERCEK 'days' gunluk 1 saatlik hacim gecmisini, Bybit'in
    kline API'sini SAYFALAYARAK ceker (tek istekte max 1000 bar geliyor,
    90 gun ~2160 bar oldugu icin birden fazla istek gerekir).
    """
    end_ms = int(time.time() * 1000)
    cutoff_ms = end_ms - days * 24 * 3600 * 1000
    total_stored = 0

    while end_ms > cutoff_ms:
        bars = fetch_klines_generic(inst_id, interval="60", limit=1000, end_time_ms=end_ms)
        if not bars:
            break
        store_volume_bars(inst_id, bars)
        total_stored += len(bars)
        oldest_ms = bars[0]["open_time_ms"]
        if oldest_ms >= end_ms:
            break  # ilerleme yok, sonsuz donguyu onle
        end_ms = oldest_ms - 1
        time.sleep(0.05)  # Bybit rate-limit'ine karsi kibar davran

    return total_stored


def _run_volume_history_seeding():
    global _volume_seed_status
    _volume_seed_status["running"] = True
    _volume_seed_status["done"] = 0
    _volume_seed_status["started_at"] = datetime.now(timezone.utc).isoformat()
    logging.info("[Hacim Seed] 3 aylik gercek hacim gecmisi doldurma basladi...")

    tickers = get_bybit_tickers()
    symbols = [t["symbol"] for t in tickers if t.get("symbol", "").endswith("USDT")]
    _volume_seed_status["total"] = len(symbols)

    ok_count = 0
    for symbol in symbols:
        try:
            stored = _fetch_full_volume_history_for_symbol(symbol)
            if stored > 0:
                ok_count += 1
        except Exception as e:
            logging.error(f"[Hacim Seed] {symbol} hata: {e}")
        _volume_seed_status["done"] += 1

    _volume_seed_status["running"] = False
    _volume_seed_status["finished_at"] = datetime.now(timezone.utc).isoformat()
    logging.info(f"[Hacim Seed] Tamamlandi. {ok_count}/{len(symbols)} sembol icin hacim gecmisi dolduruldu.")


@app.route("/seed_volume_history")
def seed_volume_history_route():
    """
    Bybit'ten GERCEK 3 aylik 1 saatlik hacim gecmisini ceker, volume_history
    tablosuna doldurur. Bir kereligine calistirilir (~10-20 dakika surer,
    arka planda). Ilerlemeyi /seed_volume_history_status'tan takip et.
    """
    key = request.args.get("key", "")
    if key != SEED_SECRET_KEY:
        return "Yetkisiz -- dogru 'key' parametresini gir.", 403
    if _volume_seed_status["running"]:
        return "Zaten calisiyor. Ilerlemeyi /seed_volume_history_status adresinden takip et.", 200
    thread = threading.Thread(target=_run_volume_history_seeding, daemon=True)
    thread.start()
    return (
        "3 aylik hacim gecmisi doldurma islemi arka planda BASLADI (10-20 dakika surebilir). "
        "Ilerlemeyi /seed_volume_history_status adresinden takip edebilirsin.",
        200,
    )


@app.route("/seed_volume_history_status")
def seed_volume_history_status_route():
    s = _volume_seed_status
    if s["started_at"] is None:
        return "Henuz hic calistirilmadi."
    durum = "CALISIYOR" if s["running"] else "TAMAMLANDI"
    return f"Durum: {durum} | Ilerleme: {s['done']}/{s['total']} | Baslangic: {s['started_at']} | Bitis: {s['finished_at']}"


def get_baseline_turnover_from_history(inst_id: str):
    """
    3 aylik gercek hacim gecmisinden (volume_history), son
    VOLUME_HISTORY_EXCLUDE_RECENT_HOURS saat HARIC, ortalama 1 saatlik
    ciroyu hesaplar. Yeterli ornek (VOLUME_HISTORY_MIN_SAMPLES) yoksa
    None doner -- seed henuz calistirilmamis olabilir.
    """
    conn = sqlite3.connect(DB_PATH)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=VOLUME_HISTORY_EXCLUDE_RECENT_HOURS)).isoformat()
    rows = conn.execute(
        "SELECT turnover FROM volume_history WHERE inst_id=? AND bar_time<=?",
        (inst_id, cutoff),
    ).fetchall()
    conn.close()
    values = [r[0] for r in rows if r[0]]
    if len(values) < VOLUME_HISTORY_MIN_SAMPLES:
        return None
    return sum(values) / len(values)


# ---------------------------------------------------------------------------
# YENI: SADE TARAMA MOTORU (Altin Kural: RSI<60 VE CVD>65)
# ---------------------------------------------------------------------------


def check_4h_trend(inst_id: str):
    """4 saatlik grafikte MA20 ve MA50 uzerinde mi -- ana trend onayi."""
    bars = fetch_klines_generic(inst_id, interval="240", limit=max(TREND_4H_MA_LONG + 5, 60))
    if bars is None or len(bars) < TREND_4H_MA_LONG:
        return False
    closes = [b["close"] for b in bars]
    ma_short = calculate_sma(closes, TREND_4H_MA_SHORT)
    ma_long = calculate_sma(closes, TREND_4H_MA_LONG)
    if ma_short is None or ma_long is None:
        return False
    current_close = closes[-1]
    return current_close > ma_short and current_close > ma_long


def run_scanner():
    """
    Ana tarama dongusu -- SADE altin kural: hacim sicramasi (3 aylik
    baseline'a gore) + tazelik filtresi + RSI<60 + CVD>65 + 4sa trend onayi.
    Hepsi ayni anda saglanirsa sinyal gonderilir.
    """
    logging.info("🔍 Bybit Hype taraması başlatılıyor...")
    logging.info(f"[Ayar Kontrolu] TELEGRAM_BOT_TOKEN tanimli mi: {bool(TELEGRAM_BOT_TOKEN)}")

    tickers = get_bybit_tickers()
    if not tickers:
        logging.warning("Bybit'ten ticker verisi alınamadı.")
        return

    checked = 0
    signals_sent = 0

    for t in tickers:
        try:
            inst_id = t.get("symbol", "")
            if not inst_id.endswith("USDT"):
                continue

            turnover_24h = float(t.get("turnover24h", 0))
            if turnover_24h < MIN_TURNOVER_24H:
                continue

            if is_recently_notified(inst_id):
                continue

            # --- 1) UCUZ ON-FILTRE: 1 saatlik mumlarla hacim + tazelik + RSI ---
            bars_1h = fetch_klines_generic(inst_id, interval="60", limit=30)
            if bars_1h is None or len(bars_1h) < 20:
                continue

            checked += 1

            # Canli tarama sirasinda cekilen barlari volume_history'e de
            # yaz -- boylece 3 aylik baseline zamanla kendiliginden guncel
            # kalir, tekrar seed calistirmaya gerek kalmaz.
            store_volume_bars(inst_id, bars_1h)

            closes_1h = [b["close"] for b in bars_1h]
            last_turnover = bars_1h[-1]["turnover"]

            baseline_turnover = get_baseline_turnover_from_history(inst_id)
            if baseline_turnover is None or baseline_turnover <= 0:
                continue  # 3 aylik baseline henuz yok (seed calistirilmamis olabilir)
            volume_ratio = last_turnover / baseline_turnover
            if volume_ratio < VOLUME_SPIKE_MIN_RATIO:
                continue

            # Tazelik filtresi: son 5 saatte cok fazla hareket etmisse UZAMIS, ele.
            if len(closes_1h) >= 5 and closes_1h[-5] > 0:
                price_change_recent_pct = (closes_1h[-1] - closes_1h[-5]) / closes_1h[-5] * 100.0
                if price_change_recent_pct > FRESHNESS_MAX_5H_CHANGE_PCT:
                    continue

            rsi = calculate_rsi(closes_1h, period=14)
            if rsi is None or rsi >= GOLDEN_RULE_RSI_MAX:
                continue

            # --- 2) PAHALI KONTROLLER: sadece ucuz filtreyi gecenler icin ---
            cvd_ratio = get_cvd_buy_ratio(inst_id)
            if cvd_ratio is None or cvd_ratio <= GOLDEN_RULE_CVD_MIN:
                record_observation(inst_id, closes_1h[-1], volume_ratio, rsi, cvd_ratio, 0)
                continue

            if not check_4h_trend(inst_id):
                record_observation(inst_id, closes_1h[-1], volume_ratio, rsi, cvd_ratio, 0)
                continue

            # --- HEPSI SAGLANDI -> SINYAL GONDER ---
            last_price = closes_1h[-1]
            msg = (
                f"🚀 *YENI TAZE HYPE SINYALI*\n\n"
                f"🪙 *Coin:* `{inst_id}`\n"
                f"⏰ *Zaman Dilimi:* 1sa Tetik + 4sa Trend Onayi\n\n"
                f"📊 *Metrikler:*\n"
                f"• Hacim Sicramasi (1sa, 3 aylik baseline'a gore): `{volume_ratio:.1f}x`\n"
                f"• Tazelik Durumu: *Taze (Temiz Giris)*\n"
                f"• 1sa RSI: `{rsi:.1f}`\n"
                f"• CVD (Alis Orani): `%{cvd_ratio*100:.0f}`\n"
                f"• 4sa Trend: *Pozitif (MA20/MA50 Ustu)*\n"
                f"• Fiyat: `{last_price}`\n\n"
                f"🟢 _Sistem uzamis hareketleri elemistir. Yatirim tavsiyesi degildir._"
            )
            send_telegram_alert(msg)
            record_observation(inst_id, last_price, volume_ratio, rsi, cvd_ratio, 1)
            signals_sent += 1
            logging.info(f"✅ Sinyal gonderildi: {inst_id} (hacim {volume_ratio:.1f}x, RSI {rsi:.1f}, CVD %{cvd_ratio*100:.0f})")

        except Exception as e:
            logging.error(f"Hata ({t.get('symbol')}): {e}")

    logging.info(f"✅ Tarama tamamlandı. {checked} coin detayli incelendi, {signals_sent} sinyal gonderildi.")


def main_loop():
    init_db()
    while True:
        try:
            run_scanner()
        except Exception as e:
            logging.error(f"Ana döngüde beklenmeyen hata: {e}")
        time.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bybit Hype Bulucu Bot (Sade Surum)")
    parser.add_argument("--gecmis", type=str, help="Belirtilen sembolun gecmis kayitlarini gosterir.")
    args = parser.parse_args()

    if args.gecmis:
        init_db()
        rows = get_symbol_history(args.gecmis)
        print(f"\n📊 {args.gecmis} - Son Gecmis Kayitlari:")
        print("-" * 90)
        for ts, price, vol_ratio, rsi, cvd, notified in rows:
            vr_str = f"{vol_ratio:.2f}x" if vol_ratio is not None else "n/a"
            rsi_str = f"{rsi:.1f}" if rsi is not None else "n/a"
            cvd_str = f"%{cvd*100:.0f}" if cvd is not None else "n/a"
            print(f"{ts[:19]} | Fiyat:{price:<12} | Hacim:{vr_str:<8} | RSI:{rsi_str:<6} | CVD:{cvd_str:<6} | Bildirildi:{bool(notified)}")
        print("-" * 90)
    else:
        flask_thread = threading.Thread(target=run_flask, daemon=True)
        flask_thread.start()
        main_loop()

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
# Bu bolum, ana tarama motoruna (run_scanner, main_loop) HICBIR sekilde
# dokunmaz -- sadece ayni Flask sunucusuna EK bir sayfa ekler. Telefondan
# https://senin-render-adresin.onrender.com/analiz adresini acip coin
# yazarak calistirilir.

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
    """
    Bybit'ten gunluk mum verisi ceker, KRONOLOJIK (en eski once) sirada
    dondurur -- Bybit API'si aksi yonde (en yeni once) verdigi icin ters
    ceviriyoruz.
    Donus: (closes, highs, lows) listeleri, hepsi ayni sirada.
    """
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
    raw = list(reversed(raw))  # kronolojik siraya cevir
    closes = [float(c[4]) for c in raw]
    highs = [float(c[2]) for c in raw]
    lows = [float(c[3]) for c in raw]
    return closes, highs, lows


def generate_technical_summary(symbol: str):
    """
    Bir coin icin RSI/MA/MACD/ATR/destek-direnc hesaplayip okunabilir bir
    Turkce ozet uretir. Bu bir tahmin/tavsiye/TP hedefi DEGILDIR -- sadece
    mevcut teknik durumun okunabilir bir ozetidir, karar tamamen kullanicinin.
    """
    closes, highs, lows = fetch_daily_klines(symbol)
    last_price = closes[-1]

    rsi = calculate_rsi(closes)
    ma20 = calculate_sma(closes, 20)
    ma50 = calculate_sma(closes, 50)
    ma200 = calculate_sma(closes, 200)
    macd = calculate_macd(closes)
    atr = calculate_atr(highs, lows, closes)
    levels = find_support_resistance(highs, lows, lookback=30)

    notlar = []

    # RSI yorumu
    if rsi is not None:
        if rsi >= 70:
            notlar.append(f"RSI {rsi:.0f} -- aşırı alım bölgesinde, kısa vadeli geri çekilme riski artmış olabilir.")
        elif rsi <= 30:
            notlar.append(f"RSI {rsi:.0f} -- aşırı satım bölgesinde, tepki yükselişi görülebilir.")
        else:
            notlar.append(f"RSI {rsi:.0f} -- nötr bölgede, belirgin bir aşırılık yok.")

    # MA (trend) yorumu
    if ma20 and ma50 and ma200:
        if last_price > ma20 > ma50 > ma200:
            notlar.append("Fiyat MA20/MA50/MA200'ün üzerinde ve sıralama yükseliş trendine işaret ediyor (güçlü trend).")
        elif last_price < ma20 < ma50 < ma200:
            notlar.append("Fiyat MA20/MA50/MA200'ün altında ve sıralama düşüş trendine işaret ediyor (zayıf trend).")
        elif last_price > ma200:
            notlar.append("Fiyat uzun vadeli ortalamanın (MA200) üzerinde ama kısa vadeli ortalamalarla karışık -- net bir trend yok.")
        else:
            notlar.append("Fiyat uzun vadeli ortalamanın (MA200) altında, genel görünüm zayıf.")

    # MACD yorumu
    if macd:
        if macd["histogram"] > 0 and macd["prev_histogram"] <= 0:
            notlar.append("MACD az önce pozitif kesişim yaptı -- yükseliş momentumu yeni başlıyor olabilir.")
        elif macd["histogram"] < 0 and macd["prev_histogram"] >= 0:
            notlar.append("MACD az önce negatif kesişim yaptı -- düşüş momentumu yeni başlıyor olabilir.")
        elif macd["histogram"] > 0:
            notlar.append("MACD pozitif -- yükseliş momentumu devam ediyor.")
        else:
            notlar.append("MACD negatif -- düşüş momentumu devam ediyor.")

    # Destek/Direnç + ATR (volatilite bandı)
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
            results_html = f"""
            <div class="card">
              <div class="row"><span class="label">Sembol</span><span>{r['symbol']}</span></div>
              <div class="row"><span class="label">Guncel Fiyat</span><span>{r['last_price']}</span></div>
              <div class="row"><span class="label">RSI (14)</span><span>{r['rsi']:.1f}</span></div>
              <div class="row"><span class="label">MA20 / MA50 / MA200</span><span>{r['ma20']:.6g} / {r['ma50']:.6g} / {r['ma200']:.6g}</span></div>
              <div class="row"><span class="label">MACD Histogram</span><span>{r['macd']['histogram']:.6g}</span></div>
            </div>
            <div class="card">
              <b>Teknik Ozet:</b>
              {notlar_html}
            </div>
            """
        except Exception as e:
            results_html = f'<div class="card err">Hata: {e}</div>'

    return ANALIZ_SAYFA_TEMPLATE.format(symbol_value=symbol, results=results_html)


def run_flask():
    """Render'ın atadığı PORT üzerinden Flask sunucusunu başlatır."""
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


# ---------------------------------------------------------------------------
# KONFİGÜRASYON VE SABİTLER
# ---------------------------------------------------------------------------
DB_PATH = "hype_history.db"

# Bybit'in resmi alternatif alan adi -- Render'dan test edilip sorunsuz
# calistigi kanitlandi (bkz. daha onceki deploy loglari).
BYBIT_BASE_URL = "https://api.bytick.com"

SCAN_INTERVAL_SECONDS = 900

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

MIN_TURNOVER_24H = 300_000.0

# --- YENI FORMUL: HACIM ANA BELIRLEYICI, FIYAT DEGISIMI IKINCIL/TEYIT EDICI ---
# Eskiden: power_score = |fiyat_degisimi| * log10(hacim)  -- fiyat agirlikliydi.
# Simdi:   power_score = hacim_orani * (1 + |fiyat_degisimi|/100)
# hacim_orani = bu coinin GUNCEL cirosu / KENDI GECMIS ORTALAMA cirosu.
# Bu, "coin normalin kac kati hacim goruyor" sorusuna cevap veriyor -- BTC/ETH
# gibi zaten hacimli coinlerin sirf buyuk olduklari icin one cikmasini onluyor,
# cunku artik mutlak hacim degil, KENDI NORMALINE GORE oran onemli.
BASELINE_MIN_SAMPLES = 20          # bu kadar gecmis kayit yoksa 'yetersiz veri' sayilir, alarm verilmez
BASELINE_EXCLUDE_RECENT_HOURS = 2.0  # son X saat baseline'a DAHIL EDILMEZ (guncel spike kendi
                                      # baseline'ini kirletmesin diye -- backtest.py'deki ayni prensip)
FRESHNESS_CHECK_VOLUME_RATIO = 2.5   # hacim orani bu degeri gecince (ekstra API cagrisi gerektiren)
                                       # tazelik kontrolu calistirilir

# NOT: Skor olcegi tamamen degisti (eskiden 60-2000 araligindaydi, simdi
# tipik olarak 1-30 araliginda olacak). Bu esik BASLANGIC degeridir --
# birkac gunluk gercek veriyle (Telegram/veritabani) kalibre edilmesi onerilir.
ALERT_POWER_SCORE_THRESHOLD = 6.0

ALERT_COOLDOWN_HOURS = 6.0

# --- YENI: OI / CVD / Tukenme kontrolu icin esikler ---
OI_CHANGE_STRONG_PCT = 5.0        # bu yuzdenin uzerinde OI artisi 'guclu yeni pozisyon girisi' sayilir
OI_CHANGE_WEAK_NEGATIVE_PCT = -3.0  # bu yuzdenin altinda OI dususu 'short squeeze' isareti sayilir
CVD_BUY_STRONG = 0.60              # bu oranin uzerinde alis baskisi 'guclu' sayilir
CVD_SELL_STRONG = 0.45             # bu oranin altinda alis orani 'zayif/satis agirlikli' sayilir
EXHAUSTION_POSITION_THRESHOLD = 0.90  # 24s bandinin bu orani uzerindeyse 'zirveye yakin' sayilir

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# ---------------------------------------------------------------------------
# VERİTABANI İŞLEMLERİ (SQLite)
# ---------------------------------------------------------------------------


def init_db():
    """SQLite veritabani ve gerekli tablo yoksa olusturur. Var olan eski
    tablolara (Render'da zaten calisan onceki surumden kalma) yeni sutunlari
    guvenli sekilde ekler -- mevcut veriyi kaybetmeden."""
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

    for col_def in [
        "oi_value REAL",
        "oi_change_pct REAL",
        "cvd_buy_ratio REAL",
        "position_in_range REAL",
        "yorum TEXT",
        "volume_ratio REAL",
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
            volume_ratio
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    """
    Yaklasik 24 saat once kaydedilmis OI degerini bulur (bu coin daha once
    kisa listeye girmisse). Bulamazsa None doner.
    """
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


def get_average_turnover(inst_id: str):
    """
    Bu coin icin KENDI GECMISINDEKI ortalama 24s ciroyu hesaplar -- yeni
    hacim-odakli formulun temeli. Guncel spike'in kendi baseline'ini
    kirletmemesi icin son BASELINE_EXCLUDE_RECENT_HOURS saat DISLANIR
    (backtest.py'de kullandigimiz ayni prensip).

    Yeterli gecmis (en az BASELINE_MIN_SAMPLES kayit) yoksa None doner --
    bu durumda bu coin icin guvenilir bir hacim orani hesaplanamaz ve
    coin alarma dahil edilmez (yanlis pozitif riskini onlemek icin).
    """
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
    """
    Bybit'in TUM linear (USDT vadeli islem) coinlerinin 24s ozetini TEK
    istekte ceker. OKX'ten farkli olarak Bybit'in ticker cevabi Open
    Interest'i de ICINDE veriyor -- ayri bir OI istegi hic gerekmiyor.
    """
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
    """
    Bybit'te OI, ticker cevabinin ICINDE zaten geliyor -- OKX'teki gibi
    AYRI bir istek yapmaya gerek yok. Bu fonksiyon sadece zaten elimizde
    olan ticker listesinden OI degerlerini cikarip sozluk haline getirir
    (kod akisini ve log mesajlarini eskisiyle tutarli tutmak icin ayri
    fonksiyon olarak birakildi).
    Donus: {symbol: oi_degeri} seklinde sozluk.
    """
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


def get_cvd_buy_ratio(inst_id: str, limit: int = 300):
    """
    Son islemlerin ne kadarinin agresif ALIS, ne kadarinin agresif SATIS
    oldugunu hesaplar. 0.5 dengeli, 1.0'a yaklastikca alis baskisi agir basar.
    Sadece kisa listeye giren (esigi gecen) adaylar icin cagrilir.
    """
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


def calculate_freshness_ratio(inst_id: str) -> float:
    """
    Son 60 Günlük (2 Ay) ve Son 2 Saatlik Hacim Kıyaslaması.
    (Mantik degismedi, sadece veri kaynagi Bybit'e cevrildi.)
    """
    try:
        url_60d = f"{BYBIT_BASE_URL}/v5/market/kline"
        res_60d = requests.get(
            url_60d, params={"category": "linear", "symbol": inst_id, "interval": "D", "limit": 60},
            timeout=10,
        ).json()
        data_60d = res_60d.get("result", {}).get("list", [])

        if len(data_60d) < 30:
            return 1.0

        # Bybit kline formati: [start, open, high, low, close, volume, turnover]
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


def generate_commentary(change_pct, oi_change_pct, cvd_ratio, position_in_range, freshness_ratio):
    """
    Sayisal sinyalleri, giris karari verirken okunabilecek kisa bir
    Turkce yorum cumlesine cevirir. Bu bir tahmin/tavsiye degildir --
    sadece o anki teknik durumun okunabilir bir ozetidir. Skoru
    ETKILEMEZ, sadece bilgilendirme amaclidir.
    """
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

    # Bybit'te OI ayri istek gerektirmiyor, ticker cevabinin icinden cikariyoruz.
    oi_map = get_all_open_interest(tickers)
    logging.info(f"[OI] {len(oi_map)} enstruman icin Open Interest verisi alindi.")

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

            # --- YENI FORMUL: hacim orani (kendi gecmisine gore) ana belirleyici ---
            avg_turnover = get_average_turnover(inst_id)
            if avg_turnover is not None and avg_turnover > 0:
                volume_ratio = turnover_24h / avg_turnover
                power_score = volume_ratio * (1 + abs(change_24h_pct) / 100.0)
            else:
                # Yeterli gecmis yok -- guvenilir bir oran hesaplanamaz.
                # Bu coin icin ALARM VERILMEZ, ama gozlem yine de kaydedilir
                # (gelecekteki taramalar icin baseline birikmeye devam etsin diye).
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
            }

            all_results.append(obs_data)

            should_notify = (
                final_score is not None
                and final_score >= ALERT_POWER_SCORE_THRESHOLD
                and not is_recently_notified(inst_id)
            )

            if should_notify:
                cvd_ratio = get_cvd_buy_ratio(inst_id)
                obs_data["cvd_buy_ratio"] = cvd_ratio

                yorum = generate_commentary(
                    change_24h_pct, oi_change_pct, cvd_ratio, position_in_range, freshness_ratio
                )
                obs_data["yorum"] = yorum
                obs_data["notified"] = 1

                alerts_to_send.append({
                    "inst_id": inst_id,
                    "price": last_price,
                    "change": change_24h_pct,
                    "turnover": turnover_24h,
                    "score": final_score,
                    "volume_ratio": volume_ratio,
                    "freshness": freshness_ratio,
                    "oi_change_pct": oi_change_pct,
                    "cvd_ratio": cvd_ratio,
                    "yorum": yorum,
                })

            record_observation(obs_data)

        except Exception as e:
            logging.error(f"Hata ({t.get('symbol')}): {e}")

    if alerts_to_send:
        msg = "🚀 *HYPE SINYALI TESPIT EDILDI!*\n\n"
        for a in alerts_to_send:
            direction = "🟢" if a["change"] >= 0 else "🔴"
            msg += f"{direction} *{a['inst_id']}*\n"
            msg += f"• Fiyat: `{a['price']}`\n"
            msg += f"• 24s Değişim: `%{a['change']:.2f}`\n"
            msg += f"• 24s Ciro: `{a['turnover']:,.0f} USDT`\n"
            msg += f"• Hacim Orani (normale gore): `{a['volume_ratio']:.2f}x`\n"
            msg += f"• Hacim İvmesi (kisa vade): `{a['freshness']:.2f}x`\n"
            if a["oi_change_pct"] is not None:
                msg += f"• OI Değişimi (24s): `%{a['oi_change_pct']:.1f}`\n"
            if a["cvd_ratio"] is not None:
                msg += f"• Alış Oranı (CVD): `%{a['cvd_ratio']*100:.0f}`\n"
            msg += f"• *Final Skor:* `{a['score']:.1f}`\n"
            msg += f"📝 _{a['yorum']}_\n\n"

        send_telegram_alert(msg)

    # None olan final_score'lari (yeterli gecmisi olmayan coinler) siralamada
    # en sona at, hata vermesinler.
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


def main_loop():
    init_db()
    while True:
        try:
            run_scanner()
        except Exception as e:
            logging.error(f"Ana döngüde beklenmeyen hata: {e}")

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

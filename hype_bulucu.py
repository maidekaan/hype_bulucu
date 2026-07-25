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
    Donus: (closes, highs, lows, turnovers) listeleri, hepsi ayni sirada.
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
    turnovers = [float(c[6]) for c in raw]
    return closes, highs, lows, turnovers


def get_funding_rate(symbol: str):
    """
    Bybit'in ticker cevabinda ZATEN gelen funding rate degerini ceker
    (ekstra endpoint gerekmiyor). Asiri pozitif/negatif funding rate,
    piyasanin cok agresif long/short'a yuklendigini gosterir -- genelde
    siddetli tersine donuslerden once gorulur.
    """
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
    """
    Bybit kullanicilarinin gercek long/short pozisyon oranini ceker.
    Donus: {'buy_ratio': 0.0-1.0, 'sell_ratio': 0.0-1.0} ya da None.
    """
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
    """
    Gunluk mum verisindeki ciro listesinden, SON GUNU disleyerek (kendi
    baseline'ini kirletmesin diye) gecmis ortalamayi hesaplar, son gunun
    cirosunu bu ortalamayla kiyaslar.
    Donus: {'today': X, 'avg_before': Y, 'ratio': Z} ya da None.
    """
    if len(turnovers) < 6:
        return None
    today = turnovers[-1]
    history = turnovers[:-1]
    avg_before = sum(history) / len(history)
    if avg_before <= 0:
        return None
    return {"today": today, "avg_before": avg_before, "ratio": today / avg_before}


def get_coingecko_market_data(symbol: str):
    """
    Bybit sembolunden (orn 'BTCUSDT') taban varlik ismini cikarip (BTC),
    CoinGecko'da arayip market cap / dolasimdaki arz bilgisini ceker.
    DIKKAT: sembol eslestirmesi bazi kucuk/yeni coinler icin basarisiz
    olabilir -- bu durumda None doner, hata firlatmaz (sayfa cokmez).
    """
    base = symbol.replace("USDT", "").strip()
    if not base:
        return None
    try:
        search_url = "https://api.coingecko.com/api/v3/search"
        r = requests.get(search_url, params={"query": base}, headers=COINGECKO_HEADERS, timeout=8)
        data = r.json()
        coins = data.get("coins", [])
        # Sembolu TAM eslesen ilk sonucu tercih et (en alakali coin genelde
        # arama sonuclarinin basinda gelir, CoinGecko piyasa degerine gore siraliyor)
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
    """
    CoinGecko'da son 24 saatte en cok ARANAN (trend olan) coinlerin
    sembol listesini ceker (buyuk harfle, orn {'BTC','PEPE',...}).
    Bu, "gercekten hype yapan" coinleri, sadece bizim hacim taramamiza
    degil, CoinGecko'nun kendi arama verisine gore de TEYIT etmemizi saglar.

    Kucuk bir cache var (varsayilan 10 dakika) -- her tarama turunda
    gereksiz yere ayni istegi tekrar tekrar atmamak icin.
    """
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
        return _trending_cache["symbols"]  # basarisiz olursa eski (varsa) veriyi kullan


def generate_technical_summary(symbol: str):
    """
    Bir coin icin RSI/MA/MACD/ATR/destek-direnc + CVD/hacim/funding/long-short/
    market cap hesaplayip okunabilir bir Turkce ozet uretir. Bu bir
    tahmin/tavsiye/TP hedefi DEGILDIR -- sadece mevcut teknik durumun
    okunabilir bir ozetidir, karar tamamen kullanicinin.
    """
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

    # Bu veri kaynaklari basarisiz olsa bile (agsal hata, sembol eslesmedi vb.)
    # sayfa COKMEMELI -- her biri kendi try/except'i ile korunuyor.
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


# ---------------------------------------------------------------------------
# BASELINE DOLDURMA (7 saatlik bekleme yerine GERCEK gecmis veriyle hizli baslangic)
# ---------------------------------------------------------------------------
# Bu bolum de ana tarama motoruna (run_scanner) DOKUNMAZ -- sadece ayni
# veritabanina, Bybit'in GERCEK 14 gunluk hacim gecmisine dayanarak baseline
# kayitlari ekler. Boylece get_average_turnover() ilk taramadan itibaren
# gecerli bir baseline bulabilir, 7 saat beklemeye gerek kalmaz.

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

            # Bybit kline formati: [start, open, high, low, close, volume, turnover]
            turnovers = [float(k[6]) for k in klines if float(k[6]) > 0]
            if len(turnovers) < 5:
                continue
            avg_turnover = sum(turnovers) / len(turnovers)

            # BASELINE_MIN_SAMPLES + pay kadar kayit ekle, hepsi
            # BASELINE_EXCLUDE_RECENT_HOURS'un OTESINE (yani 'gecerli baseline'
            # sayilacak bolgeye) yayilmis zaman damgalariyla.
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
        time.sleep(0.1)  # Bybit rate-limit'ine karsi kibar davran

    conn.close()
    _seed_status["running"] = False
    _seed_status["finished_at"] = datetime.now(timezone.utc).isoformat()
    logging.info(f"[Seed] Tamamlandi. {ok_count}/{len(symbols)} sembol icin baseline dolduruldu.")


@app.route("/seed_baseline")
def seed_baseline_route():
    """
    Telefondan/tarayicidan bir kez ziyaret edilir:
    https://senin-adresin.onrender.com/seed_baseline?key=GIZLI_ANAHTARIN
    Arka planda calisir (birkac dakika surer), sayfa hemen doner.
    Ilerlemeyi /seed_status adresinden takip edebilirsin.
    """
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

# /seed_baseline adresini yetkisiz kullanimdan korumak icin basit bir anahtar.
# Render'da Environment Variable olarak SEED_SECRET_KEY tanimlayip kendi
# gizli degerini belirlemen onerilir -- tanimlamazsan varsayilan kullanilir
# (herkese acik URL oldugu icin bunu degistirmen guvenlik acisindan iyi olur).
SEED_SECRET_KEY = os.environ.get("SEED_SECRET_KEY", "degistir-bu-anahtari")

# CoinGecko Demo API Key -- market cap ve trending ozellikleri icin.
# 2026'da CoinGecko ucretsiz kullanim icin bile bu key'i zorunlu kildi.
# Render'da Environment Variable olarak COINGECKO_API_KEY tanimlaman gerekiyor.
COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY", "")
COINGECKO_HEADERS = {"x-cg-demo-api-key": COINGECKO_API_KEY} if COINGECKO_API_KEY else {}

SCAN_INTERVAL_SECONDS = 900

# --- 4 saatlik ozet raporu ayarlari ---
REPORT_INTERVAL_SECONDS = 4 * 3600     # her 4 saatte bir ozet raporu gonder
REPORT_MIN_MOVE_PCT = 10.0              # rapora girecek min. 4 saatlik hareket yuzdesi
REPORT_MAX_ITEMS_SHOWN = 15             # mesajda en fazla kac coin listelenecek (uzun mesaj limiti icin)

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

    # CoinGecko Trending listesini TEK istekte cek (cache'li, her tarama
    # turunda yeniden istek atmiyor -- 10 dakikada bir yenileniyor).
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

                # CoinGecko Trending ile capraz teyit -- bu coin hem bizim
                # hacim taramamizda HEM CoinGecko'nun kendi arama trendinde
                # cikiyorsa, bu guclu bir capraz dogrulamadir.
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


# ---------------------------------------------------------------------------
# 4 SAATLIK OZET RAPORU
# ---------------------------------------------------------------------------
# Bu bolum run_scanner()'a HICBIR sekilde dokunmaz -- sadece zaten biriken
# veriyi (her taramada TUM taranan coinler icin kaydedilen fiyat/notified
# bilgisi) okuyup periyodik bir ozet Telegram mesaji uretir.

def generate_4h_report():
    """
    Son ~4 saatte biriken gozlemlerden, her coin icin O PENCEREDEKI ilk ve
    son fiyati kiyaslayarak GERCEK 4 saatlik fiyat degisimini hesaplar
    (Bybit'in kendi '24s degisim' alanindan FARKLI bir hesap -- bu bizim
    kendi biriktirdigimiz veriden, tam 4 saatlik pencere icin).
    Ayrica o coin icin bu pencerede alarm verilip verilmedigini (notified)
    kontrol eder.
    Donus: buyukten kucuge siralanmis mover listesi.
    """
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
    """4 saatlik ozet raporunu Telegram'a gonderir."""
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
    last_report_ts = time.time()  # ilk rapor, deploy'dan 4 saat sonra gonderilir
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

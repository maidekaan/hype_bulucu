import argparse
import logging
import math
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
import requests
from flask import Flask

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


def run_flask():
    """Render'ın atadığı PORT üzerinden Flask sunucusunu başlatır."""
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


# ---------------------------------------------------------------------------
# KONFİGÜRASYON VE SABİTLER
# ---------------------------------------------------------------------------
DB_PATH = "hype_history.db"

SCAN_INTERVAL_SECONDS = 900

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

MIN_TURNOVER_24H = 300_000.0
ALERT_POWER_SCORE_THRESHOLD = 60.0
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
            oi_value, oi_change_pct, cvd_buy_ratio, position_in_range, yorum
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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


def get_symbol_history(inst_id: str, limit: int = 10):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT timestamp, last_price, change_24h_pct, turnover_24h, power_score,
               freshness_ratio, final_score, oi_change_pct, cvd_buy_ratio, yorum
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
# OKX BORSASI VERİ ÇEKME VE HESAPLAMA MANTIĞI
# ---------------------------------------------------------------------------


def get_okx_swap_tickers():
    url = "https://www.okx.com/api/v5/market/tickers?instType=SWAP"
    try:
        response = requests.get(url, timeout=15)
        data = response.json()
        if data.get("code") == "0":
            return data.get("data", [])
    except Exception as e:
        logging.error(f"OKX Tickers çekilirken hata: {e}")
    return []


def get_all_open_interest():
    """
    TUM SWAP enstrumanlarinin Open Interest'ini TEK istekte ceker.
    Donus: {instId: oi_degeri} seklinde sozluk.
    """
    url = "https://www.okx.com/api/v5/public/open-interest?instType=SWAP"
    try:
        response = requests.get(url, timeout=15)
        data = response.json()
        if data.get("code") == "0":
            result = {}
            for item in data.get("data", []):
                try:
                    result[item["instId"]] = float(item.get("oiCcy") or item.get("oi") or 0)
                except (TypeError, ValueError):
                    continue
            return result
    except Exception as e:
        logging.error(f"OKX Open Interest çekilirken hata: {e}")
    return {}


def get_cvd_buy_ratio(inst_id: str, limit: int = 300):
    """
    Son islemlerin ne kadarinin agresif ALIS, ne kadarinin agresif SATIS
    oldugunu hesaplar. 0.5 dengeli, 1.0'a yaklastikca alis baskisi agir basar.
    Sadece kisa listeye giren (esigi gecen) adaylar icin cagrilir.
    """
    url = f"https://www.okx.com/api/v5/market/trades?instId={inst_id}&limit={limit}"
    try:
        response = requests.get(url, timeout=10)
        data = response.json()
        if data.get("code") != "0":
            return None
        trades = data.get("data", [])
        if not trades:
            return None
        buy_vol = sum(float(t["sz"]) for t in trades if t.get("side") == "buy")
        sell_vol = sum(float(t["sz"]) for t in trades if t.get("side") == "sell")
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
    (Bu fonksiyona dokunulmadi, oldugu gibi korundu.)
    """
    try:
        url_60d = f"https://www.okx.com/api/v5/market/candles?instId={inst_id}&bar=1D&limit=60"
        res_60d = requests.get(url_60d, timeout=10).json()
        data_60d = res_60d.get("data", [])

        if len(data_60d) < 30:
            return 1.0

        total_60d_vol = sum([float(c[7]) for c in data_60d])
        avg_hourly_vol_60d = (total_60d_vol / len(data_60d)) / 24.0

        url_2h = f"https://www.okx.com/api/v5/market/candles?instId={inst_id}&bar=1H&limit=2"
        res_2h = requests.get(url_2h, timeout=10).json()
        data_2h = res_2h.get("data", [])

        if len(data_2h) < 2:
            return 1.0

        total_2h_vol = sum([float(c[7]) for c in data_2h])
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
    logging.info("🔍 OKX Hype taraması başlatılıyor...")
    logging.info(f"[Ayar Kontrolu] TELEGRAM_BOT_TOKEN tanimli mi: {bool(TELEGRAM_BOT_TOKEN)}")
    logging.info(f"[Ayar Kontrolu] TELEGRAM_CHAT_ID tanimli mi: {bool(TELEGRAM_CHAT_ID)}")

    tickers = get_okx_swap_tickers()
    if not tickers:
        logging.warning("OKX'ten ticker verisi alınamadı.")
        return

    oi_map = get_all_open_interest()
    logging.info(f"[OI] {len(oi_map)} enstruman icin Open Interest verisi alindi.")

    alerts_to_send = []
    all_results = []

    for t in tickers:
        try:
            inst_id = t.get("instId", "")
            if not inst_id.endswith("-USDT-SWAP"):
                continue

            last_price = float(t.get("last", 0))
            open_24h = float(t.get("sodUtc0", t.get("open24h", last_price)))
            high_24h = float(t.get("high24h", last_price))
            low_24h = float(t.get("low24h", last_price))

            if open_24h == 0:
                continue

            change_24h_pct = ((last_price - open_24h) / open_24h) * 100.0
            turnover_24h = float(t.get("volCcy24h", 0))

            if turnover_24h < MIN_TURNOVER_24H:
                continue

            power_score = abs(change_24h_pct) * math.log10(turnover_24h)

            freshness_ratio = 1.0
            if power_score > 30.0:
                freshness_ratio = calculate_freshness_ratio(inst_id)

            final_score = power_score * freshness_ratio

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
            }

            all_results.append(obs_data)

            should_notify = (
                final_score >= ALERT_POWER_SCORE_THRESHOLD
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
                    "freshness": freshness_ratio,
                    "oi_change_pct": oi_change_pct,
                    "cvd_ratio": cvd_ratio,
                    "yorum": yorum,
                })

            record_observation(obs_data)

        except Exception as e:
            logging.error(f"Hata ({t.get('instId')}): {e}")

    if alerts_to_send:
        msg = "🚀 *HYPE SINYALI TESPIT EDILDI!*\n\n"
        for a in alerts_to_send:
            direction = "🟢" if a["change"] >= 0 else "🔴"
            msg += f"{direction} *{a['inst_id']}*\n"
            msg += f"• Fiyat: `{a['price']}`\n"
            msg += f"• 24s Değişim: `%{a['change']:.2f}`\n"
            msg += f"• 24s Ciro: `{a['turnover']:,.0f} USDT`\n"
            msg += f"• Hacim İvmesi: `{a['freshness']:.2f}x`\n"
            if a["oi_change_pct"] is not None:
                msg += f"• OI Değişimi (24s): `%{a['oi_change_pct']:.1f}`\n"
            if a["cvd_ratio"] is not None:
                msg += f"• Alış Oranı (CVD): `%{a['cvd_ratio']*100:.0f}`\n"
            msg += f"• *Final Skor:* `{a['score']:.1f}`\n"
            msg += f"📝 _{a['yorum']}_\n\n"

        send_telegram_alert(msg)

    top_candidates = sorted(all_results, key=lambda x: x["final_score"], reverse=True)[:5]
    if top_candidates:
        logging.info("📊 Şu Anki En Yüksek Skorlu 5 Coin:")
        for c in top_candidates:
            oi_str = f"{c['oi_change_pct']:.1f}%" if c.get("oi_change_pct") is not None else "n/a"
            logging.info(
                f"   -> {c['inst_id']:<18} | Değişim: %{c['change_24h_pct']:<6.2f} | "
                f"İvme: {c['freshness_ratio']:<4.2f}x | OI: {oi_str:<8} | Skor: {c['final_score']:.1f}"
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
        description="OKX Hype Bulucu Bot ve Veritabanı Sorgulayıcı"
    )
    parser.add_argument(
        "--gecmis",
        type=str,
        help="Belirtilen sembolün geçmiş veritabanı kayıtlarını gösterir. Örn: ONDO-USDT-SWAP",
    )

    args = parser.parse_args()

    if args.gecmis:
        init_db()
        rows = get_symbol_history(args.gecmis)
        print(f"\n📊 {args.gecmis} - Son Geçmiş Kayıtları:")
        print("-" * 100)
        for r in rows:
            ts, price, chg, turnover, power, fresh, final, oi_chg, cvd, yorum = r
            oi_str = f"%{oi_chg:.1f}" if oi_chg is not None else "n/a"
            cvd_str = f"%{cvd*100:.0f}" if cvd is not None else "n/a"
            print(f"{ts[:19]} | Fiyat:{price:<10.4f} | 24s%:{chg:<7.2f} | Skor:{final:<8.1f} "
                  f"| OI:{oi_str:<7} | CVD:{cvd_str:<6}")
            if yorum:
                print(f"    Not: {yorum}")
        print("-" * 100)
    else:
        flask_thread = threading.Thread(target=run_flask, daemon=True)
        flask_thread.start()
        main_loop()

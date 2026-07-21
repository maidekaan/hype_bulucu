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

# Flask'in varsayılan log kalabalığını engellemek için
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

# Taramalar arası bekleme süresi (Saniye) - 15 Dakika
SCAN_INTERVAL_SECONDS = 900

# Telegram Ayarları (Render Environment Variables üzerinden okunur)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Filtreleme Parametreleri (Daha fazla sinyal görebilmek için optimize edildi)
MIN_TURNOVER_24H = 300_000.0  # En az 300k USDT 24s ciro
ALERT_POWER_SCORE_THRESHOLD = (
    60.0  # Telegram bildirim eşiği (Hassasiyet artırıldı)
)
ALERT_COOLDOWN_HOURS = 6.0  # Aynı coin için tekrar bildirim cooldown süresi

# Logging Yapılandırması
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# ---------------------------------------------------------------------------
# VERİTABANI İŞLEMLERİ (SQLite)
# ---------------------------------------------------------------------------


def init_db():
    """SQLite veritabanı ve gerekli tablo yoksa oluşturur."""
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
    conn.commit()
    conn.close()


def record_observation(data: dict):
    """Hesaplanan metriği veritabanına kaydeder."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO hype_observations (
            timestamp, inst_id, last_price, change_24h_pct, turnover_24h,
            power_score, freshness_ratio, final_score, notified
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        ),
    )
    conn.commit()
    conn.close()


def is_recently_notified(inst_id: str) -> bool:
    """Belirtilen coin için son COOLDOWN saat içinde bildirim atıldı mı kontrol eder."""
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


def get_symbol_history(inst_id: str, limit: int = 10):
    """Geçmiş veritabanı kayıtlarını terminale basar (--gecmis parametresi için)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT timestamp, last_price, change_24h_pct, turnover_24h, power_score, freshness_ratio, final_score
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
    """Telegram Bot API üzerinden mesaj gönderir."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning(
            "Telegram Token veya Chat ID bulunamadı. Bildirim atlanıyor."
        )
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
    """OKX borsasındaki tüm SWAP (Vadeli) paritelerin 24s verilerini çeker."""
    url = "https://www.okx.com/api/v5/market/tickers?instType=SWAP"
    try:
        response = requests.get(url, timeout=15)
        data = response.json()
        if data.get("code") == "0":
            return data.get("data", [])
    except Exception as e:
        logging.error(f"OKX Tickers çekilirken hata: {e}")
    return []


def get_okx_candles(inst_id: str, bar: str = "1H", limit: int = 24):
    """İlgili enstrümanın mum verilerini çeker (Freshness Ratio hesabı için)."""
    url = f"https://www.okx.com/api/v5/market/candles?instId={inst_id}&bar={bar}&limit={limit}"
    try:
        response = requests.get(url, timeout=10)
        data = response.json()
        if data.get("code") == "0":
            return data.get("data", [])
    except Exception as e:
        logging.error(f"{inst_id} mum verisi çekilirken hata: {e}")
    return []


def calculate_freshness_ratio(candles):
    """
    Mum verilerine göre Hacim İvmesini hesaplar:
    Son 4 saatlik ortalama hacim / Önceki 20 saatlik ortalama hacim.
    """
    if len(candles) < 24:
        return 1.0

    try:
        volumes = [float(c[7]) for c in candles[:24]]
        recent_4h = sum(volumes[:4]) / 4.0
        older_20h = sum(volumes[4:24]) / 20.0

        if older_20h == 0:
            return 1.0

        return recent_4h / older_20h
    except Exception:
        return 1.0


def run_scanner():
    """Ana piyasa tarama ve hesaplama döngüsü."""
    logging.info("🔍 OKX Hype taraması başlatılıyor...")
    tickers = get_okx_swap_tickers()
    if not tickers:
        logging.warning("OKX'ten ticker verisi alınamadı.")
        return

    alerts_to_send = []
    all_results = []  # Terminal çıktısı için tüm sonuçlar

    for t in tickers:
        try:
            inst_id = t.get("instId", "")
            if not inst_id.endswith("-USDT-SWAP"):
                continue

            last_price = float(t.get("last", 0))
            open_24h = float(t.get("sodUtc0", t.get("open24h", last_price)))

            if open_24h == 0:
                continue

            change_24h_pct = ((last_price - open_24h) / open_24h) * 100.0
            turnover_24h = float(t.get("volCcy24h", 0))

            if turnover_24h < MIN_TURNOVER_24H:
                continue

            power_score = abs(change_24h_pct) * math.log10(turnover_24h)

            freshness_ratio = 1.0
            if power_score > 30.0:  # Mum analiz eşiği
                candles = get_okx_candles(inst_id, bar="1H", limit=24)
                freshness_ratio = calculate_freshness_ratio(candles)

            final_score = power_score * freshness_ratio

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
            }

            all_results.append(obs_data)

            should_notify = 0
            if (
                final_score >= ALERT_POWER_SCORE_THRESHOLD
                and not is_recently_notified(inst_id)
            ):
                should_notify = 1
                obs_data["notified"] = 1
                alerts_to_send.append({
                    "inst_id": inst_id,
                    "price": last_price,
                    "change": change_24h_pct,
                    "turnover": turnover_24h,
                    "score": final_score,
                    "freshness": freshness_ratio,
                })

            record_observation(obs_data)

        except Exception as e:
            logging.error(f"Hata ({t.get('instId')}): {e}")

    # Toplu Bildirim Gönderimi (Telegram)
    if alerts_to_send:
        msg = "🚀 *HYPE SINYALI TESPIT EDILDI!*\n\n"
        for a in alerts_to_send:
            direction = "🟢" if a["change"] >= 0 else "🔴"
            msg += f"{direction} *{a['inst_id']}*\n"
            msg += f"• Fiyat: `{a['price']}`\n"
            msg += f"• 24s Değişim: `%{a['change']:.2f}`\n"
            msg += f"• 24s Ciro: `{a['turnover']:,.0f} USDT`\n"
            msg += f"• Hacim İvmesi (Freshness): `{a['freshness']:.2f}x`\n"
            msg += f"• *Final Skor:* `{a['score']:.1f}`\n\n"

        send_telegram_alert(msg)

    # Terminal Çıktısı (Anlık Piyasa Liderleri)
    top_candidates = sorted(
        all_results, key=lambda x: x["final_score"], reverse=True
    )[:5]
    if top_candidates:
        logging.info("📊 Şu Anki En Yüksek Skorlu 5 Coin:")
        for c in top_candidates:
            logging.info(
                f"   -> {c['inst_id']:<18} | Değişim: %{c['change_24h_pct']:<6.2f} | Skor: {c['final_score']:.1f}"
            )

    logging.info("✅ Tarama tamamlandı.")


# ---------------------------------------------------------------------------
# ANA ÇALIŞTIRMA DÖNGÜSÜ
# ---------------------------------------------------------------------------


def main_loop():
    """Botun periyodik olarak tarama yapmasını sağlayan ana döngü."""
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
        rows = get_symbol_history(args.gecmis)
        print(f"\n📊 {args.gecmis} - Son Geçmiş Kayıtları:")
        print(
            "----------------------------------------------------------------------------------"
        )
        print(
            f"{'Tarih (UTC)':<20} | {'Fiyat':<10} | {'24s %':<8} | {'Power Score':<12} | {'Final Skor':<10}"
        )
        print(
            "----------------------------------------------------------------------------------"
        )
        for r in rows:
            print(
                f"{r[0][:19]:<20} | {r[1]:<10.4f} | {r[2]:<8.2f} | {r[3]:<12.1f} | {r[6]:<10.1f}"
            )
    else:
        # 1. Render web sunucusunu arka planda başlat
        flask_thread = threading.Thread(target=run_flask, daemon=True)
        flask_thread.start()

        # 2. Ana tarama döngüsünü çalıştır
        main_loop()
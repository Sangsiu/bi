import os
import logging
import json
import re
from datetime import datetime
from typing import Dict, List, Optional

from curl_cffi import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters
)

from keep_alive import keep_alive

# =========================
# LOGGING
# =========================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# =========================
# CONFIG MANAGER
# =========================
class ConfigManager:
    def __init__(self, config_file='config.json'):
        self.config_file = config_file
        self.provinces_ref = {
            "11": "ACEH", "12": "SUMATERA UTARA", "13": "SUMATERA BARAT",
            "31": "DKI JAKARTA", "32": "JAWA BARAT", "33": "JAWA TENGAH",
            "34": "D.I. YOGYAKARTA", "35": "JAWA TIMUR", "36": "BANTEN",
            "51": "BALI", "52": "NTB", "53": "NTT",
            "61": "KALBAR", "62": "KALTENG", "63": "KALSEL",
            "64": "KALTIM", "65": "KALTARA",
            "71": "SULUT", "72": "SULTENG", "73": "SULSEL",
            "74": "SULTRA", "75": "GORONTALO", "76": "SULBAR",
            "81": "MALUKU", "82": "MALUT",
            "91": "PAPUA BARAT", "94": "PAPUA"
        }
        self.config = self.load_config()

    def load_config(self):
        if os.path.exists(self.config_file):
            with open(self.config_file, 'r') as f:
                return json.load(f)
        default = {"province_id": 31}
        self.save_config(default)
        return default

    def save_config(self, config):
        with open(self.config_file, 'w') as f:
            json.dump(config, f, indent=4)
        self.config = config
        return True

    def get_province_id(self):
        return self.config.get("province_id", 31)

    def set_province_id(self, prov_id):
        self.config["province_id"] = prov_id
        return self.save_config(self.config)

    def get_province_name(self, prov_id):
        return self.provinces_ref.get(str(prov_id), f"PROV {prov_id}")


# =========================
# BI SLOT EXTRACTOR
# =========================
class BISlotExtractor:
    def __init__(self, province_id=31):
        self.base_url = "https://pintar.bi.go.id"
        self.list_url = f"{self.base_url}/Order/ListKasKeliling?provinceId={province_id}"
        self.api_url = f"{self.base_url}/Order/GetKasKelByProvinceNew"
        self.province_id = province_id
        self.token = ""

        self.session = requests.Session()
        self.session.headers.update({
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
        })

    # =========================
    # TOKEN
    # =========================
    def refresh_token(self):
        try:
            r = self.session.get(self.list_url, impersonate="chrome124", timeout=30)
            logger.info(f"[TOKEN] status={r.status_code}")

            ct = r.headers.get("content-type", "")
            logger.info(f"[TOKEN] content-type={ct}")

            snippet = (r.text or "")[:500].lower()

            if "waitingroom" in snippet or "captcha" in snippet or "cf" in snippet:
                logger.warning("[TOKEN] Cloudflare / Waiting Room detected!")

            token_match = re.search(
                r'__RequestVerificationToken.*?value="([^"]+)"',
                r.text
            )

            if token_match:
                self.token = token_match.group(1)
                logger.info("[TOKEN] Found ✅")
                return True

            logger.warning("[TOKEN] Not Found ❌")
            logger.warning(r.text[:300])
            return False

        except Exception as e:
            logger.exception(f"[TOKEN ERROR] {e}")
            return False

    # =========================
    # DATA
    # =========================
    def get_all_data(self):
        if not self.token and not self.refresh_token():
            return []

        payload = {
            "draw": 1,
            "start": 0,
            "length": 100,
            "provId": self.province_id,
            "__RequestVerificationToken": self.token
        }

        headers = {
            "x-requested-with": "XMLHttpRequest",
            "referer": self.list_url
        }

        try:
            r = self.session.post(
                self.api_url,
                data=payload,
                headers=headers,
                impersonate="chrome124",
                timeout=30
            )

            logger.info(f"[DATA] status={r.status_code}")
            ct = r.headers.get("content-type", "")
            logger.info(f"[DATA] content-type={ct}")

            if "application/json" not in ct:
                logger.warning("[DATA] Not JSON response ❌")
                logger.warning(r.text[:300])
                return []

            result = r.json()
            logger.info(f"[DATA] Keys: {list(result.keys())}")

            return result.get("data", [])

        except Exception as e:
            logger.exception(f"[DATA ERROR] {e}")
            return []

    # =========================
    # PROCESS
    # =========================
    def process_data(self):
        raw = self.get_all_data()
        processed = []

        for item in raw:
            slots = []
            total = 0

            for s in item.get("SlotList", []):
                waktu_text = "N/A"
                waktu_id = "N/A"

                for v in s.values():
                    if isinstance(v, str):
                        if any(t in v.upper() for t in ["WIB", "WITA", "WIT"]):
                            waktu_text = v
                        elif len(v) == 36 and "-" in v:
                            waktu_id = v

                sisa = s.get("SisaQuota", 0)
                total += sisa

                if waktu_text != "N/A":
                    slots.append({
                        "waktu": waktu_text,
                        "sisa": sisa,
                        "waktu_id": waktu_id
                    })

            if slots:
                processed.append({
                    "lokasi": item.get("Lokasi", "N/A"),
                    "tanggal": item.get("OpenDateToString", "N/A"),
                    "kaskel_id": item.get("KaskelId", "N/A"),
                    "total": total,
                    "slots": slots
                })

        return processed


# =========================
# BOT LOGIC
# =========================
class BISlotBot:
    def __init__(self):
        self.config = ConfigManager()

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        prov_id = self.config.get_province_id()
        prov_name = self.config.get_province_name(prov_id)

        text = (
            "👋 *BI Slot Monitor*\n\n"
            f"📍 Wilayah: `{prov_name}`\n\n"
            "Ketik ID provinsi untuk ganti.\n"
            "Tekan tombol di bawah:"
        )

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📊 Detail Slot", callback_data="slot")],
            [InlineKeyboardButton("📋 Ringkasan", callback_data="ringkasan")]
        ])

        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)

    async def show_slot(self, update, context):
        query = update.callback_query
        await query.answer()

        await query.message.reply_text("🔍 Mengambil data...")

        prov_id = self.config.get_province_id()
        extractor = BISlotExtractor(prov_id)
        data = extractor.process_data()

        if not data:
            await query.message.reply_text("❌ Data kosong / kemungkinan diblok server.")
            return

        result = f"📊 *DETAIL SLOT*\n\n"
        for item in data:
            result += f"📍 *{item['lokasi']}*\n"
            result += f"📅 {item['tanggal']}\n"
            result += f"Total: {item['total']}\n\n"

        await query.message.reply_text(result, parse_mode="Markdown")

    async def callback_handler(self, update, context):
        query = update.callback_query
        if query.data == "slot":
            await self.show_slot(update, context)


# =========================
# RUN
# =========================
if __name__ == "__main__":
    BOT_TOKEN = os.environ.get("BOT_TOKEN")

    if not BOT_TOKEN:
        print("❌ BOT_TOKEN tidak ditemukan!")
    else:
        keep_alive()

        bot_logic = BISlotBot()
        app = Application.builder().token(BOT_TOKEN).build()

        app.add_handler(CommandHandler("start", bot_logic.start))
        app.add_handler(CallbackQueryHandler(bot_logic.callback_handler))

        print("✅ Bot Running (Replit Mode)")
        app.run_polling()

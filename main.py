import os
import logging
import json
import re
from datetime import datetime
from typing import Dict, List, Optional

# Library pihak ketiga
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

# Integrasi Keep Alive untuk Replit
from keep_alive import keep_alive

# Logging setup
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
            "11": "ACEH", "12": "SUMATERA UTARA", "13": "SUMATERA BARAT", "14": "RIAU", "15": "JAMBI",
            "16": "SUMATERA SELATAN", "17": "BENGKULU", "18": "LAMPUNG", "19": "KEP. BANGKA BELITUNG",
            "20": "KEP. RIAU", "31": "DKI JAKARTA", "32": "JAWA BARAT", "33": "JAWA TENGAH",
            "34": "D.I. YOGYAKARTA", "35": "JAWA TIMUR", "36": "BANTEN", "51": "BALI",
            "52": "NUSA TENGGARA BARAT", "53": "NUSA TENGGARA TIMUR", "61": "KALIMANTAN BARAT",
            "62": "KALIMANTAN TENGAH", "63": "KALIMANTAN SELATAN", "64": "KALIMANTAN TIMUR",
            "65": "KALIMANTAN UTARA", "71": "SULAWESI UTARA", "72": "SULAWESI TENGAH",
            "73": "SULAWESI SELATAN", "74": "SULAWESI TENGGARA", "75": "GORONTALO",
            "76": "SULAWESI BARAT", "81": "MALUKU", "82": "MALUKU UTARA", "91": "PAPUA BARAT", "94": "PAPUA"
        }
        self.config = self.load_config()
    
    def load_config(self):
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r') as f:
                    return json.load(f)
            default_config = {"province_id": 31, "max_items_per_page": 5}
            self.save_config(default_config)
            return default_config
        except Exception as e:
            logger.error(f"Error loading config: {e}")
            return {"province_id": 31, "max_items_per_page": 5}
    
    def save_config(self, config):
        try:
            with open(self.config_file, 'w') as f:
                json.dump(config, f, indent=4)
            self.config = config
            return True
        except Exception as e:
            logger.error(f"Error saving config: {e}")
            return False
    
    def get_province_name(self, prov_id):
        return self.provinces_ref.get(str(prov_id), f"PROVINSI ID {prov_id}")
    
    def set_province_id(self, prov_id):
        self.config['province_id'] = prov_id
        return self.save_config(self.config)
    
    def get_province_id(self):
        return self.config.get('province_id', 31)

# =========================
# BI SLOT EXTRACTOR
# =========================
class BISlotExtractor:
    def __init__(self, province_id=31):
        self.base_url = "https://pintar.bi.go.id"
        self.list_url = f"{self.base_url}/Order/ListKasKeliling?provinceId={province_id}"
        self.api_url = f"{self.base_url}/Order/GetKasKelByProvinceNew"
        self.province_id = province_id
        self.session = requests.Session()
        self.session.headers.update({"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        self.token = ""

    def refresh_token(self):
        try:
            r = self.session.get(self.list_url, impersonate="chrome124", timeout=30)
            token_match = re.search(r'__RequestVerificationToken.*?value="([^"]+)"', r.text)
            if token_match:
                self.token = token_match.group(1)
                return True
            return False
        except Exception as e:
            logger.error(f"Token error: {e}")
            return False

    def get_all_data(self):
        if not self.token and not self.refresh_token(): return []
        payload = {"draw": 1, "start": 0, "length": 100, "provId": self.province_id, "__RequestVerificationToken": self.token}
        headers = {"x-requested-with": "XMLHttpRequest"}
        try:
            r = self.session.post(self.api_url, data=payload, headers=headers, impersonate="chrome124", timeout=30)
            return r.json().get("data", [])
        except Exception:
            return []

    def process_data(self):
        data = self.get_all_data()
        processed = []
        for item in data:
            slots = []
            total_lokasi = 0
            for s in item.get("SlotList", []):
                w_id, w_text = "N/A", "N/A"
                for k, v in s.items():
                    if isinstance(v, str):
                        if any(tz in v.upper() for tz in ["WIB", "WITA", "WIT"]):
                            w_text = v.strip()
                        elif len(v) == 36 and "-" in v: 
                            w_id = v
                sisa = s.get("SisaQuota", 0)
                total_lokasi += sisa
                if w_text != "N/A":
                    slots.append({'waktu': w_text, 'sisa': sisa, 'waktu_id': w_id})
            if slots:
                processed.append({
                    'lokasi': item.get("Lokasi", "N/A"),
                    'kaskel_id': item.get("KaskelId", "N/A"),
                    'tanggal': item.get("OpenDateToString", "N/A"),
                    'total': total_lokasi,
                    'slots': slots
                })
        return processed

# =========================
# BOT HANDLERS
# =========================
class BISlotBot:
    def __init__(self):
        self.config_manager = ConfigManager()

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        prov_id = self.config_manager.get_province_id()
        prov_name = self.config_manager.get_province_name(prov_id)
        msg = (
            "👋 *BI Slot Monitor (PINTAR)*\n\n"
            f"📍 *Wilayah:* `{prov_name}`\n"
            "Ketik ID provinsi apa saja untuk ganti wilayah.\n\n"
            "Pilih menu:"
        )
        keyboard = [
            [InlineKeyboardButton("📊 Detail Slot", callback_data="menu_slot")],
            [InlineKeyboardButton("📋 Ringkasan", callback_data="menu_ringkasan")],
            [InlineKeyboardButton("🌍 List ID Provinsi", callback_data="menu_provinsi")]
        ]
        if update.callback_query:
            await update.callback_query.message.edit_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

    async def setprov_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            raw_input = context.args[0] if context.args else update.message.text.strip()
            if not raw_input.isdigit(): return
            
            prov_id = int(raw_input)
            prov_name = self.config_manager.get_province_name(prov_id)
            
            if self.config_manager.set_province_id(prov_id):
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("📊 Cek Slot Sekarang", callback_data="menu_slot")]])
                await update.message.reply_text(
                    f"✅ **Wilayah Berhasil Diubah!**\n📍 Sekarang memantau: `{prov_name}`", 
                    parse_mode='Markdown', reply_markup=kb
                )
        except Exception:
            pass

    async def show_slot_page(self, update_or_query, context, page=1):
        is_cb = hasattr(update_or_query, 'data')
        msg_obj = update_or_query.message if is_cb else update_or_query
        
        status = await msg_obj.reply_text("🔍 *Mengambil Data & ID...*", parse_mode='Markdown')
        prov_id = self.config_manager.get_province_id()
        data = BISlotExtractor(prov_id).process_data()

        if not data:
            await status.edit_text(f"❌ Tidak ada slot tersedia untuk {self.config_manager.get_province_name(prov_id)}.")
            return

        items_per_page = 5
        total_pages = (len(data) + items_per_page - 1) // items_per_page
        start = (page - 1) * items_per_page
        page_data = data[start:start + items_per_page]

        res = f"📊 *DETAIL SLOT & ID* (Hal {page}/{total_pages})\n📍 *{self.config_manager.get_province_name(prov_id)}*\n"
        res += "—" * 15 + "\n"
        
        for item in page_data:
            res += f"📍 *{item['lokasi']}*\n📅 {item['tanggal']} | 🆔 ` {item['kaskel_id']} `\n"
            res += "```\n"
            for s in item['slots']:
                res += f"🕒 {s['waktu']} (Sisa: {s['sisa']})\n"
                res += f"🆔 {s['waktu_id']}\n"
                res += "—" * 10 + "\n"
            res += "```\n"
        
        nav = []
        if page > 1: nav.append(InlineKeyboardButton("⬅️", callback_data=f"page_{page-1}"))
        nav.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
        if page < total_pages: nav.append(InlineKeyboardButton("➡️", callback_data=f"page_{page+1}"))
        
        kb = InlineKeyboardMarkup([nav, [InlineKeyboardButton("🏠 Menu Utama", callback_data="back_to_menu")]])
        await status.delete()
        await msg_obj.reply_text(res, parse_mode='Markdown', reply_markup=kb)

    async def ringkasan_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        is_cb = hasattr(update, 'data')
        msg_obj = update.message if is_cb else update
        status = await msg_obj.reply_text("⌛ *Menghitung...*", parse_mode='Markdown')
        prov_id = self.config_manager.get_province_id()
        data = BISlotExtractor(prov_id).process_data()
        
        if not data:
            await status.edit_text("❌ Kosong.")
            return
            
        summary = f"📋 *RINGKASAN* - {self.config_manager.get_province_name(prov_id)}\n\n"
        for i in data:
            summary += f"• {i['lokasi']}: *{i['total']}*\n"
        
        await status.delete()
        await msg_obj.reply_text(summary, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Menu", callback_data="back_to_menu")]]))

    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        if query.data == "menu_slot": await self.show_slot_page(query, context, 1)
        elif query.data == "menu_ringkasan": await self.ringkasan_command(query, context)
        elif query.data == "back_to_menu": await self.start(update, context)
        elif query.data == "menu_provinsi":
            plist = self.config_manager.provinces_ref
            txt = "🌍 *Daftar ID Provinsi PINTAR*\n\n"
            count = 0
            for k, v in plist.items():
                txt += f"• `{k}` : {v}\n"
                count += 1
                if count == 25: break
            txt += "\n💡 Ketik ID provinsi mana saja untuk ganti."
            await query.message.edit_text(txt, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Kembali", callback_data="back_to_menu")]]))
        elif query.data.startswith("page_"):
            page = int(query.data.split("_")[1])
            await query.message.delete()
            await self.show_slot_page(query, context, page)

# =========================
# RUNNER
# =========================
if __name__ == "__main__":
    # Gunakan Secret BOT_TOKEN di Replit
    BOT_TOKEN = os.environ.get('BOT_TOKEN')
    
    if not BOT_TOKEN:
        print("❌ Error: BOT_TOKEN tidak ditemukan di Environment Variables!")
    else:
        # Jalankan Keep Alive Server
        keep_alive()
        
        # Inisialisasi Bot
        bot_logic = BISlotBot()
        app = Application.builder().token(BOT_TOKEN).build()

        # Handlers
        app.add_handler(CommandHandler("start", bot_logic.start))
        app.add_handler(CommandHandler("setprov", bot_logic.setprov_handler))
        app.add_handler(MessageHandler(filters.Regex(r'^\d{1,2}$'), bot_logic.setprov_handler))
        app.add_handler(CallbackQueryHandler(bot_logic.button_callback))

        print("✅ Bot is online on Replit...")
        app.run_polling()
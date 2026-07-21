import os
import joblib
import requests
from pathlib import Path
from flask import Flask, request, jsonify
from supabase import create_client, Client
from dotenv import load_dotenv
from datetime import datetime
from datetime import timedelta
import dateutil.parser 


# ============================================================
# PEMBACAAN .ENV SECARA ABSOLUT
# ============================================================
# 1. Dapatkan lokasi folder dari file index.py ini (/api)
CURRENT_DIR = Path(__file__).resolve().parent

# 2. Naik satu tingkat ke folder root (/skripsi-bintang)
ROOT_DIR = CURRENT_DIR.parent

# 3. Gabungkan dengan nama file .env
ENV_PATH = ROOT_DIR / ".env"

# 4. Load env secara eksplisit
load_dotenv(dotenv_path=ENV_PATH)

app = Flask(__name__)

# ============================================================
# KONFIGURASI ENVIRONMENT VARIABLES
# (Akan diisi melalui dashboard Vercel)
# ============================================================
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")

# Validasi awal untuk mencegah crash jika env belum diatur
if SUPABASE_URL and SUPABASE_KEY:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
else:
    supabase = None

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# ============================================================
# LOAD MODEL & ENCODER (Hanya 1x saat cold start Vercel)
# ============================================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
try:
    model = joblib.load(os.path.join(BASE_DIR, 'model_rf_v5.pkl'))
    encoder_jenis = joblib.load(os.path.join(BASE_DIR, 'encoder_jenis_v5.pkl'))
except Exception as e:
    print(f"Error loading model: {e}")
    model, encoder_jenis = None, None

# Mapping sesuai dengan kebutuhan training Anda
MAPPING_JENIS = {
    "Kulit" : "Leather",
    "Mesh"  : "Mesh",
    "Kanvas": "Canvas"
}

def send_telegram_message(chat_id, text):
    """Helper function untuk menembak API Telegram"""
    url = f"{TELEGRAM_API_URL}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    requests.post(url, json=payload, timeout=5)

@app.route('/api/device_online', methods=['POST'])
def device_online():
    try:
        # Ambil payload suhu dan kelembapan dari ESP32 saat booting
        data = request.get_json() or {}
        suhu = float(data.get('suhu', 0))
        kelembapan = float(data.get('kelembapan', 0))

        if not supabase:
            return jsonify({'error': 'Koneksi database terputus'}), 500

        # 1. Cek apakah ada sesi pengeringan yang masih 'aktif'
        response = supabase.table("sesi_pengeringan").select("*").eq("status", "aktif").order("id", desc=True).limit(1).execute()

        if response.data:
            # ==========================================
            # SKENARIO A: RECOVERY MODE AKTIF
            # ==========================================
            sesi = response.data[0]
            chat_id = sesi["chat_id"]
            record_id = sesi["id"]
            jenis_sepatu = sesi["jenis_sepatu"]
            
            # Formatting Waktu (Konversi UTC ke WIB +7)
            dt_buat = dateutil.parser.isoparse(sesi["created_at"]) + timedelta(hours=7)
            dt_update = dateutil.parser.isoparse(sesi["updated_at"]) + timedelta(hours=7)
            
            # Prediksi Ulang Berdasarkan Sensor Terbaru
            jenis_en = MAPPING_JENIS.get(jenis_sepatu, "Canvas")
            jenis_enc = encoder_jenis.transform([jenis_en])[0]
            features = [[jenis_enc, suhu, kelembapan]]
            prediksi_baru = int(model.predict(features)[0])

            # Update database dengan waktu prediksi hasil recovery
            supabase.table("sesi_pengeringan").update({
                "waktu_prediksi": prediksi_baru,
                "sisa_waktu": prediksi_baru,
                "suhu_sekarang": suhu,
                "kelembapan_sekarang": kelembapan
            }).eq("id", record_id).execute()

            pesan = (
                f"⚠️ *RECOVERY MODE: SISTEM DIPULIHKAN!*\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"Sesi sebelumnya terputus akibat alat mati/restart.\n\n"
                f"👟 Jenis : {jenis_sepatu}\n"
                f"🕒 Mulai Awal : {dt_buat.strftime('%d-%m-%Y %H:%M')}\n"
                f"🛑 Terakhir Aktif : {dt_update.strftime('%d-%m-%Y %H:%M')}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🔄 Prediksi Baru : {prediksi_baru} menit\n"
                f"(Dihitung ulang berdasarkan suhu saat ini: {suhu}°C)"
            )
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": pesan, "parse_mode": "Markdown"})

            # Balas ke ESP32 agar melanjutkan pengeringan
            return jsonify({
                "status": "resume",
                "waktu_menit": prediksi_baru,
                "jenis_sepatu": jenis_sepatu
            }), 200

        else:
            # ==========================================
            # SKENARIO B: NORMAL BOOT (STANDBY)
            # ==========================================
            res_chat = supabase.table("sesi_pengeringan").select("chat_id").order("id", desc=True).limit(1).execute()
            if not res_chat.data:
                return jsonify({"status": "standby"}), 200
                
            chat_id = res_chat.data[0]["chat_id"]

            pesan = (
                "👋 *Selamat datang di SMART SHOE DRYER!*\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                "Sistem siap digunakan. Silakan pilih jenis sepatu:\n\n"
                "👟 *Mesh* — Sepatu kain/rajut\n"
                "🧵 *Kanvas* — Sepatu kanvas\n"
                "🥾 *Kulit* — Sepatu kulit\n"
                "📊 *Status* — Lihat status pengering"
            )
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": pesan, "parse_mode": "Markdown"})

            return jsonify({"status": "standby"}), 200

    except Exception as e:
        print(f"Error Device Online: {e}")
        return jsonify({"error": str(e)}), 500

# ============================================================
# ROUTE 0: HEALTH CHECK (DEFAULT HOME)
# ============================================================
@app.route('/', methods=['GET'])
def home():
    return jsonify({
        "status": "online",
        "message": "Smart Shoe Dryer API is running perfectly on Vercel Serverless!",
        "version": "1.0"
    }), 200

# ============================================================
# ROUTE 1: WEBHOOK TELEGRAM
# Menerima input dari pengguna dan menyimpan status ke Supabase
# ============================================================

@app.route('/api/webhook_telegram', methods=['POST'])
def telegram_webhook():
    update = request.get_json()
    
    # Validasi Input Telegram
    if not update or "message" not in update or "text" not in update["message"]:
        return jsonify({"status": "ok"}), 200
    
    chat_id = update["message"]["chat"]["id"]
    text = update["message"]["text"].strip()

    # Parsing input pengguna (Pilih Sepatu)
    jenis = None
    if "Mesh" in text: jenis = "Mesh"
    elif "Kanvas" in text: jenis = "Kanvas"
    elif "Kulit" in text: jenis = "Kulit"

    if jenis:
        try:
            data = {
                "chat_id": chat_id,
                "jenis_sepatu": jenis,
                "status": "menunggu_sensor"
            }
            if supabase:
                supabase.table("sesi_pengeringan").insert(data).execute()
            
            send_telegram_message(
                chat_id, 
                f"👟 Jenis sepatu *{jenis}* dipilih.\n\n⏳ Menunggu mesin pengering dinyalakan dan mengirim data sensor..."
            )
            return jsonify({"status": "success", "message": f"Pesanan {jenis} masuk antrean"}), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    
    # Command: Start
    elif text == "/start":
        send_telegram_message(
            chat_id, 
            "👋 *Selamat datang di SMART SHOE DRYER!*\n\nSilakan ketik atau pilih dari keyboard:\n👟 Mesh\n🧵 Kanvas\n👢 Kulit"
        )
        return jsonify({"status": "success"}), 200
        
    # Command: Batal
    elif text in ["❌ Batal", "/cancel", "Batal"]:
        response = supabase.table("sesi_pengeringan").select("id, status").eq("chat_id", chat_id).in_("status", ["aktif", "menunggu_sensor"]).execute()
        
        if len(response.data) > 0:
            supabase.table("sesi_pengeringan").update({"status": "dibatalkan"}).eq("chat_id", chat_id).in_("status", ["aktif", "menunggu_sensor"]).execute()
            pesan = "🛑 Proses pengeringan berhasil DIBATALKAN. Mesin akan segera dimatikan."
        else:
            pesan = "⚠️ Tidak ada proses pengeringan yang sedang berjalan."
            
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": pesan})
        return jsonify({"status": "success"}), 200
    
    # Command: History / Show
    elif text.lower() in ["/show", "history-all", "show"]:
        response = supabase.table("sesi_pengeringan").select("*").eq("chat_id", chat_id).order("id", desc=True).limit(5).execute()
        
        if len(response.data) > 0:
            pesan = "📚 *5 RIWAYAT PENGERINGAN TERAKHIR*\n━━━━━━━━━━━━━━━━━━\n"
            for row in response.data:
                # Blok anti-crash untuk waktu
                try:
                    if row.get("created_at"):
                        dt_buat = dateutil.parser.isoparse(row["created_at"]) + timedelta(hours=7)
                        waktu = dt_buat.strftime('%d %b %H:%M')
                    else:
                        waktu = "Waktu tdk tercatat"
                except Exception:
                    waktu = "Format error"
                    
                jenis_sepatu = row.get("jenis_sepatu", "-")
                status = row.get("status", "unknown")
                durasi = row.get("waktu_prediksi_total", "-")
                
                ikon = "✅" if status == "selesai" else "❌" if status == "dibatalkan" else "🔄"
                pesan += f"{ikon} *{jenis_sepatu}* ({durasi} mnt)\n   📅 {waktu} | Status: {status.title()}\n\n"
        else:
            pesan = "📭 Belum ada riwayat pengeringan."
            
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": pesan, "parse_mode": "Markdown"})
        return jsonify({"status": "success"}), 200

    # Command: Status
    elif text in ["📊 Status", "Status", "/status"]:
        response = supabase.table("sesi_pengeringan").select("*").eq("chat_id", chat_id).order("id", desc=True).limit(1).execute()
        
        if len(response.data) > 0:
            sesi = response.data[0]
            if sesi["status"] == "aktif":
                suhu = sesi.get("suhu_sekarang", "Menunggu data...")
                kelembapan = sesi.get("kelembapan_sekarang", "Menunggu data...")
                sisa = sesi.get("sisa_waktu", "Menunggu data...")
                relay_nyala = sesi.get("relay_menyala")
                
                ikon_relay = "🔥 Mengeringkan" if relay_nyala else "🌡️ Menunggu Suhu Turun"
                jenis_sepatu = sesi.get("jenis_sepatu", "-")

                pesan = (
                    f"📊 *STATUS SMART SHOE DRYER*\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"👟 Jenis : {jenis_sepatu}\n"
                    f"🌡️ Suhu : {suhu} °C\n"
                    f"💧 Lembap : {kelembapan} %\n"
                    f"⏳ Sisa Waktu : {sisa} menit\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"🔌 Status Mesin : {ikon_relay}"
                )
            elif sesi["status"] == "menunggu_sensor":
                pesan = "⏳ Mesin sedang memanaskan dan melakukan kalkulasi ML. Tunggu sebentar..."
            else:
                pesan = "💤 Mesin dalam keadaan Standby.\nKetik /start untuk memulai pengeringan baru."
        else:
            pesan = "💤 Belum ada riwayat pengeringan.\nKetik /start untuk memulai."

        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": chat_id, "text": pesan, "parse_mode": "Markdown"})
        return jsonify({"status": "success"}), 200 

    # SAFETY NET: Jika command tidak dikenali
    return jsonify({"status": "ignored", "message": "Command tidak dikenali"}), 200

# ============================================================
# ROUTE 2: API UNTUK ESP32
# Menerima data sensor, mengecek antrean, melakukan prediksi
# ============================================================
@app.route('/api/predict', methods=['POST'])
def predict_esp32():
    if not model or not encoder_jenis:
        return jsonify({'error': 'Model ML gagal dimuat di server'}), 500

    data = request.get_json()
    if not data or 'suhu_sekarang' not in data or 'kelembapan_sekarang' not in data:
        return jsonify({'error': 'Payload tidak valid'}), 400

    suhu = float(data['suhu_sekarang'])
    kelembapan = float(data['kelembapan_sekarang'])

    # 1. Cari antrean pengeringan di Supabase
    if not supabase:
        return jsonify({'error': 'Koneksi database terputus'}), 500
        
    response = supabase.table("sesi_pengeringan").select("*").eq("status", "menunggu_sensor").order("updated_at", desc=True).limit(1).execute()
    sesi = response.data
    

    # Jika tidak ada user yang request via Telegram, abaikan data sensor ESP32
    if not sesi:
        return jsonify({"status": "ignored", "message": "Tidak ada sesi menunggu dari Telegram"}), 200

    sesi_aktif = sesi[0]
    jenis_sepatu = sesi_aktif["jenis_sepatu"]
    chat_id = sesi_aktif["chat_id"]
    record_id = sesi_aktif["id"]

    try:
        # 2. Proses Machine Learning
        jenis_en = MAPPING_JENIS.get(jenis_sepatu, "Canvas") # Fallback safety
        jenis_enc = encoder_jenis.transform([jenis_en])[0]
        
        # Urutan fitur harus sama dengan X_train
        features = [[jenis_enc, suhu, kelembapan]]
        prediksi_waktu = int(model.predict(features)[0])

        # 3. Update status database menjadi proses_kering
        supabase.table("sesi_pengeringan").update({
            "status": "aktif",
            "suhu_sekarang": suhu,
            "kelembapan_sekarang": kelembapan,
            "waktu_prediksi": prediksi_waktu
        }).eq("id", record_id).execute()

        # 4. Kirim notifikasi final ke pengguna Telegram
        pesan = (
            f"✅ *PENGERING AKTIF!*\n"
            f"───────────────\n"
            f"👟 Jenis    : {jenis_sepatu}\n"
            f"🌡️ Suhu     : {suhu} °C\n"
            f"💧 Lembap   : {kelembapan} %\n"
            f"🔮 Prediksi : {prediksi_waktu} menit\n"
            f"───────────────\n"
            f"🔥 Pengering sedang berjalan!"
        )
        send_telegram_message(chat_id, pesan)

        # 5. Balas ESP32
        return jsonify({
            "status": "success", 
            "waktu_menit": prediksi_waktu,
            "jenis_sepatu": jenis_sepatu
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/telemetry', methods=['POST'])
def telemetry():
    data = request.json
    
    # Update data pada sesi yang sedang berjalan (aktif)
    # Asumsi: Saat ini hanya ada 1 alat yang berjalan pada satu waktu
    supabase.table("sesi_pengeringan").update({
        "suhu_sekarang": data.get("suhu"),
        "kelembapan_sekarang": data.get("kelembapan"),
        "sisa_waktu": data.get("sisa_waktu"),
        "relay_menyala": data.get("relay_menyala")
    }).eq("status", "aktif").execute()
    
    return jsonify({"status": "updated"}), 200

# ==========================================
# ROUTE KHUSUS TRIGGER SELESAI DARI ESP32
# ==========================================
@app.route('/api/notify_done', methods=['POST'])
def notify_done():
    if not supabase:
        return jsonify({'error': 'Koneksi database terputus'}), 500

    # Cari sesi yang saat ini sedang aktif
    response = supabase.table("sesi_pengeringan").select("*").eq("status", "aktif").execute()
    
    if len(response.data) > 0:
        sesi = response.data[0]
        chat_id = sesi["chat_id"]
        jenis = sesi.get("jenis_sepatu", "Anda")
        record_id = sesi["id"]
        
        # 1. Update status database menjadi selesai
        supabase.table("sesi_pengeringan").update({
            "status": "selesai",
            "sisa_waktu": 0,
            "relay_menyala": False
        }).eq("id", record_id).execute()
        
        # 2. Kirim pesan notifikasi elegan ke Telegram
        pesan = (
            f"✨ *PENGERINGAN SELESAI* ✨\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Sepatu {jenis} telah kering sempurna.\n"
            f"Mesin telah dimatikan secara otomatis.\n\n"
            f"Terima kasih telah menggunakan Smart Shoe Dryer."
        )
        send_telegram_message(chat_id, pesan)
        
        return jsonify({"status": "success", "message": "Notifikasi terkirim"}), 200
        
    return jsonify({"status": "ignored", "message": "Tidak ada sesi aktif"}), 200

# ==========================================
# ROUTE KHUSUS TRIGGER STATUS BERKALA (30 MENIT)
# ==========================================
@app.route('/api/notify_status', methods=['POST'])
def notify_status():
    if not supabase:
        return jsonify({'error': 'Koneksi database terputus'}), 500

    # Tarik data sesi yang sedang berjalan
    response = supabase.table("sesi_pengeringan").select("*").eq("status", "aktif").execute()
    
    if len(response.data) > 0:
        sesi = response.data[0]
        chat_id = sesi["chat_id"]
        
        suhu = sesi.get("suhu_sekarang", "-")
        kelembapan = sesi.get("kelembapan_sekarang", "-")
        sisa = sesi.get("sisa_waktu", "-")
        relay_nyala = sesi.get("relay_menyala")
        jenis = sesi.get("jenis_sepatu", "-")
        
        # Penamaan status yang representatif
        ikon_relay = "🔥 Memanaskan" if relay_nyala else "🌡️ Menstabilkan Suhu"
        
        # Tampilan UI Minimalis dan Elegan untuk notifikasi berkala
        pesan = (
            f"⏱️ *UPDATE BERKALA (30 MENIT)*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"👟 Jenis : {jenis}\n"
            f"🌡️ Suhu : {suhu} °C\n"
            f"💧 Lembap : {kelembapan} %\n"
            f"⏳ Sisa Waktu : {sisa} menit\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🔌 Kondisi : {ikon_relay}"
        )
        
        send_telegram_message(chat_id, pesan)
        return jsonify({"status": "success", "message": "Notifikasi berkala terkirim"}), 200
        
    return jsonify({"status": "ignored", "message": "Tidak ada sesi aktif"}), 200

# Untuk testing lokal
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
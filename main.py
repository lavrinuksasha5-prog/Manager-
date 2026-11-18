import os, uuid, zipfile, random, hashlib, traceback
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, String, Integer, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime
from telethon import TelegramClient
from opentele2.td import TDesktop
from opentele2.api import API, UseCurrentSession

# --- Конфиг ---
DATA_DIR = Path("/data")
SESS_DIR = DATA_DIR / "sessions"
UPLOAD_DIR = Path("/app/uploads")
SESS_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

DEVICES = [
    {"device_model": "Windows 10 x64", "system_version": "Windows 10", "app_version": "4.14.4 x64"},
    {"device_model": "Xiaomi SU7", "system_version": "Android 14", "app_version": "10.14.1"},
    {"device_model": "iPad Pro", "system_version": "iOS 17.5", "app_version": "10.14.1"},
]

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
    status = Column(String, default="pending")
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
    if not line or line.startswith("#"):
        return None

    proxy_type = "socks5"
    if line.startswith("socks5://"):
        line = line.replace("socks5://", "")
    elif line.startswith("http://"):
        line = line.replace("http://", "")
        proxy_type = "http"
    elif line.startswith("https://"):
        line = line.replace("https://", "")
        proxy_type = "http"

    user = passw = None

    if "@" in line:
        auth, addr = line.split("@", 1)
        if ":" in auth:
            user, passw = auth.split(":", 1)
        host, port = addr.split(":")
    elif line.count(":") == 3:
        host, port, user, passw = line.split(":")
    elif line.count(":") == 1:
        host, port = line.split(":")
    else:
        return None

    return {
        "proxy_type": proxy_type,
        "addr": host,
        "port": int(port),
        "username": user,
        "password": passw,
    }

def pop_free_proxy(db):
    p = db.query(Proxy).filter_by(busy=0).first()
    if not p:
        return None
    p.busy = 1
    db.commit()
    return {
        "proxy_type": p.proxy_type,
        "addr": p.addr,
        "port": p.port,
        "username": p.username,
        "password": p.password,
    }

# --- tdata парсер ---
def extract_tdata(zip_path: str) -> list:
    out = UPLOAD_DIR / uuid.uuid4().hex
    out.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(out)
    found = []
    for path in out.rglob("*"):
        if not path.is_dir():
            continue
        if (path / "key_datas").exists() or any(
            f.name.isupper() and len(f.name) == 16 for f in path.glob("*")
        ):
            found.append(str(path))
    return found

# --- Валидатор ---
async def validate(tdata_path: str, proxy: dict, acc_id: str):
    tdesk = TDesktop(tdata_path)
    session = str(SESS_DIR / f"{acc_id}.session")
    
    print(f"[DEBUG] Конвертация tdata: {tdata_path}")
    
    # ИСПРАВЛЕНО: Убраны неверные аргументы из Generate()
    client = await tdesk.ToTelethon(
        session, 
        UseCurrentSession,
        api=API.TelegramDesktop.Generate(unique_id=acc_id)
    )
    
    if proxy:
        print(f"[DEBUG] Подключаем прокси: {proxy['addr']}:{proxy['port']}")
        client._proxy = proxy
    else:
        print("[WARN] Прокси не найден, подключаемся напрямую")

    await client.connect()
    try:
        if not await client.is_user_authorized():
            print("[DEBUG] Сессия не авторизована (invalid)")
            return {"status": "invalid"}
        
        me = await client.get_me()
        print(f"[DEBUG] Успех! Аккаунт: {me.username or me.phone}")
        return {
            "status": "valid",
            "user_id": me.id,
            "username": me.username,
            "phone": me.phone,
            "device": "Telegram Desktop", 
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
    for td in dirs:
        acc_id = str(uuid.uuid4())
        proxy = pop_free_proxy(db)
        print(f"[TEST] Обработка аккаунта {acc_id} | tdata={td}")
        try:
            res = await validate(td, proxy, acc_id)
            db.add(Account(
                id=acc_id,
                user_id=res.get("user_id", 0),
                username=res.get("username"),
                phone=res.get("phone"),
                device=res.get("device"),
                proxy=f"{proxy['addr']}:{proxy['port']}" if proxy else "direct",
                status=res["status"],
            ))
        except Exception as e:
            print(f"[ERR] Ошибка на аккаунте {acc_id}:")
            traceback.print_exc()
            db.add(Account(id=acc_id, status="error", proxy="direct"))
        db.commit()
    db.close()

@app.get("/")
def root():
    return FileResponse("index.html")

@app.post("/proxies-text")
async def upload_proxies_text(data: ProxyList):
    db = SessionDB()
    added = 0
    for line in data.text.splitlines():
        p = parse_proxy(line)
        if p:
            db.add(Proxy(
                raw=line.strip(),
                addr=p["addr"],
                port=p["port"],
                username=p["username"],
                password=p["password"],
                proxy_type=p["proxy_type"],
            ))
            added += 1
    db.commit()
    db.close()
    return {"status": "ok", "added": added}

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
        "id": r.id, "user_id": r.user_id, "username": r.username,
        "phone": r.phone, "device": r.device, "proxy": r.proxy,
        "status": r.status, "created_at": str(r.created_at),
    } for r in rows]

@app.delete("/accounts/{acc_id}")
def delete_account(acc_id: str):
    db = SessionDB()
    db.query(Account).filter_by(id=acc_id).delete()
    db.commit(); db.close()
    return {"status": "deleted", "id": acc_id}

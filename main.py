import os, uuid, zipfile, random, hashlib
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, Column, String, Integer, DateTime
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime
from telethon import TelegramClient
from tdata_reader import TDataReader

# --- Конфиг ---
API_ID = 2040
API_HASH = "b18441a1ff607e10a989891a5462e627"
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

Base.metadata.create_all(engine)

# --- Прокси (sticky) ---
def load_pool():
    pool = []
    path = Path("proxies.txt")
    if not path.exists():
        return pool
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        p = line.split(":")
        pool.append({
            "proxy_type": "socks5",
            "addr": p[0], "port": int(p[1]),
            "username": p[2] if len(p) > 2 else None,
            "password": p[3] if len(p) > 3 else None,
        })
    return pool

def sticky_proxy(acc_id: str, pool: list):
    if not pool:
        return None
    idx = int(hashlib.md5(acc_id.encode()).hexdigest(), 16) % len(pool)
    return pool[idx]

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
    reader = TDataReader(tdata_path)
    acc = reader.read()
    dev = random.choice(DEVICES)
    session = str(SESS_DIR / f"{acc_id}.session")
    client = TelegramClient(
        session, API_ID, API_HASH, proxy=proxy,
        device_model=dev["device_model"],
        system_version=dev["system_version"],
        app_version=dev["app_version"],
    )
    client.session.set_dc(acc.dc_id, "149.154.167.51", 443)
    client.session.auth_key = acc.auth_key
    await client.connect()
    try:
        if not await client.is_user_authorized():
            return {"status": "invalid"}
        me = await client.get_me()
        return {
            "status": "valid",
            "user_id": acc.user_id,
            "username": me.username,
            "phone": me.phone,
            "device": dev["device_model"],
        }
    finally:
        await client.disconnect()

# --- API ---
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

async def process_zip(zip_path: str):
    dirs = extract_tdata(zip_path)
    pool = load_pool()
    db = SessionDB()
    for td in dirs:
        acc_id = str(uuid.uuid4())
        proxy = sticky_proxy(acc_id, pool)
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
            print(f"[ERR] {acc_id}: {e}")
            db.add(Account(id=acc_id, status="error", proxy="direct"))
        db.commit()
    db.close()

@app.get("/")
def root():
    return {"status": "ok", "service": "TWKS Manager"}

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

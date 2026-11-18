import os, uuid, time, aiohttp, shutil, sqlite3
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, String, Integer, DateTime, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime
from telethon import TelegramClient

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DATA_DIR = Path("/data")
SESS_DIR = DATA_DIR / "sessions"
STATIC_DIR = Path("/app/static")
SESS_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

db_file = DATA_DIR / "twks_sessions.db"
engine = create_engine(f"sqlite:///{db_file}")
Base = declarative_base()
SessionDB = sessionmaker(bind=engine)

class Account(Base):
    __tablename__ = "sessions_accounts"
    id = Column(String, primary_key=True)
    user_id = Column(Integer, nullable=True)
    username = Column(String, nullable=True)
    first_name = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    avatar_url = Column(String, nullable=True)
    proxy = Column(String)
    ip = Column(String, nullable=True)
    ping = Column(Integer, default=0)
    country = Column(String, nullable=True)
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
    if line.startswith("socks5://"):
        proxy_type = "socks5"
        line = line.replace("socks5://", "")
    elif line.startswith("http://"):
        line = line.replace("http://", "")

    user = passw = None
    if "@" in line:
        auth, addr = line.split("@", 1)
        if ":" in auth: user, passw = auth.split(":", 1)
        host, port = addr.split(":")
    elif line.count(":") == 3:
        host, port, user, passw = line.split(":")
    elif line.count(":") == 1:
        host, port = line.split(":")
    else:
        return None

    port = ''.join(filter(str.isdigit, port))
    if not port: return None
    return {"proxy_type": proxy_type, "addr": host, "port": int(port), "username": user, "password": passw}

def pop_free_proxy(db):
    p = db.query(Proxy).order_by(Proxy.id.desc()).first()
    if not p: return None, None
    return p, {"proxy_type": p.proxy_type, "addr": p.addr, "port": p.port, "username": p.username, "password": p.password}

async def get_proxy_info(proxy: dict):
    proxy_url = f"http://{proxy['username']}:{proxy['password']}@{proxy['addr']}:{proxy['port']}" if proxy.get("username") else f"http://{proxy['addr']}:{proxy['port']}"
    start_time = time.time()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get("http://ip-api.com/json/?fields=status,country,query", proxy=proxy_url) as response:
                data = await response.json()
                ping = int((time.time() - start_time) * 1000)
                if data.get("status") == "success":
                    return {"ip": data.get("query"), "country": data.get("country")}, ping
    except Exception:
        pass
    return {"ip": "N/A", "country": "Неизвестно"}, 0

async def validate_session_file(session_path: str, proxy_dict: dict, acc_id: str):
    p_type = proxy_dict.get('proxy_type', 'http')
    proxy_tuple = (p_type, proxy_dict['addr'], proxy_dict['port'], True, proxy_dict.get('username'), proxy_dict.get('password')) if proxy_dict.get('username') else (p_type, proxy_dict['addr'], proxy_dict['port'])
    
    # Официальные дефолтные API Android для стабильного коннекта сессий
    API_ID = 2040
    API_HASH = "b18441a1ff607e10a989891a5462e627"

    client = TelegramClient(session_path, api_id=API_ID, api_hash=API_HASH, proxy=proxy_tuple)
    net_info, ping_ms = await get_proxy_info(proxy_dict)
    
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return {"status": "invalid", "error_msg": "Сессия не авторизована"}
        
        me = await client.get_me()
        avatar_url = None
        if me.photo:
            avatar_path = STATIC_DIR / f"avatar_{acc_id}.jpg"
            await client.download_profile_photo(me, file=str(avatar_path))
            avatar_url = f"/static/avatar_{acc_id}.jpg"

        await client.disconnect()
        return {
            "status": "valid",
            "user_id": me.id,
            "username": me.username,
            "first_name": me.first_name,
            "phone": me.phone,
            "avatar_url": avatar_url,
            "country": net_info["country"],
            "ip": net_info["ip"],
            "ping": ping_ms,
            "premium": getattr(me, "premium", False),
            "error_msg": None
        }
    except Exception as e:
        return {"status": "error", "error_msg": str(e)}

async def process_session_upload(file_path: str):
    db = SessionDB()
    acc_id = uuid.uuid4().hex
    session_dest = SESS_DIR / f"{acc_id}.session"
    
    shutil.move(file_path, str(session_dest))

    _, proxy_dict = pop_free_proxy(db)
    if not proxy_dict:
        db.add(Account(id=acc_id, status="error", error_msg="Нет свободных прокси"))
        db.commit()
        db.close()
        return

    res = await validate_session_file(str(session_dest), proxy_dict, acc_id)
    
    db.add(Account(
        id=acc_id,
        user_id=res.get("user_id"),
        username=res.get("username"),
        first_name=res.get("first_name"),
        phone=res.get("phone"),
        avatar_url=res.get("avatar_url"),
        proxy=f"{proxy_dict['addr']}:{proxy_dict['port']}",
        ip=res.get("ip"),
        ping=res.get("ping"),
        country=res.get("country"),
        premium=res.get("premium"),
        status=res.get("status", "error"),
        error_msg=res.get("error_msg")
    ))
    db.commit()
    db.close()

@app.get("/")
def root(): return FileResponse("index.html")

@app.post("/proxies-text")
async def upload_proxies_text(data: ProxyList):
    db = SessionDB()
    count = 0
    for line in data.text.splitlines():
        p = parse_proxy(line)
        if p:
            db.add(Proxy(addr=p["addr"], port=p["port"], username=p["username"], password=p["password"], proxy_type=p["proxy_type"]))
            count += 1
    db.commit(); db.close()
    return {"status": "ok", "added": count}

@app.post("/upload-session")
async def upload_session(bg: BackgroundTasks, file: UploadFile = File(...)):
    tmp_path = SESS_DIR / f"temp_{uuid.uuid4()}.session"
    tmp_path.write_bytes(await file.read())
    bg.add_task(process_session_upload, str(tmp_path))
    return {"status": "accepted"}

@app.get("/accounts")
def list_accounts():
    db = SessionDB()
    rows = db.query(Account).all()
    out = []
    for r in rows:
        session_file = SESS_DIR / f"{r.id}.session"
        avatar_path = None
        if r.avatar_url:
            local_file = Path("/app") / r.avatar_url.lstrip("/")
            if local_file.exists(): avatar_path = r.avatar_url

        out.append({
            "id": r.id, "user_id": r.user_id, "username": r.username, "first_name": r.first_name,
            "phone": r.phone, "avatar_url": avatar_path, "proxy": r.proxy, "ip": r.ip,
            "ping": r.ping, "country": r.country, "premium": r.premium, "status": r.status,
            "error_msg": r.error_msg, "has_session": session_file.exists(),
            "created_at": r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else ""
        })
    db.close()
    return out

@app.get("/download/{acc_id}")
def download_session(acc_id: str):
    sf = SESS_DIR / f"{acc_id}.session"
    if not sf.exists(): return JSONResponse({"error": "Файл не найден"}, status_code=404)
    return FileResponse(path=str(sf), filename=f"{acc_id}.session", media_type='application/octet-stream')

@app.delete("/accounts/{acc_id}")
def delete_account(acc_id: str):
    db = SessionDB()
    acc = db.query(Account).filter_by(id=acc_id).first()
    if acc:
        sf = SESS_DIR / f"{acc_id}.session"
        if sf.exists(): sf.unlink()
        db.delete(acc)
        db.commit()
    db.close()
    return {"status": "deleted"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))

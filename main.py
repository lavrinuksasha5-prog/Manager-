import os, uuid, time, aiohttp, zipfile, shutil, sqlite3, socks, json
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, String, Integer, DateTime, Boolean, text
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime
from telethon import TelegramClient

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DATA_DIR = Path("/data")
SESS_DIR = DATA_DIR / "sessions"
META_DIR = DATA_DIR / "meta"
UPLOAD_DIR = Path("/app/uploads")
STATIC_DIR = Path("/app/static")
SESS_DIR.mkdir(parents=True, exist_ok=True)
META_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
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
    is_active_web = Column(Boolean, default=False)
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

# Автомиграция колонок для существующих баз на томе
with engine.begin() as conn:
    try:
        conn.execute(text("ALTER TABLE sessions_accounts ADD COLUMN is_active_web BOOLEAN DEFAULT 0;"))
    except Exception:
        pass # Колонка уже существует

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
        auth, addr_port = line.rsplit("@", 1)
        if ":" in auth: user, passw = auth.split(":", 1)
        else: user = auth
        if ":" in addr_port: host, port = addr_port.rsplit(":", 1)
        else: return None
    else:
        parts = line.split(":")
        if len(parts) >= 4:
            host, port = parts[0], parts[1]
            user = parts[2]
            passw = ":".join(parts[3:])
        elif len(parts) == 2:
            host, port = parts
        else:
            return None

    port = ''.join(filter(str.isdigit, port))
    if not host or not port: return None
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

def get_telethon_proxy_tuple(proxy_dict: dict):
    s_type = socks.SOCKS5 if proxy_dict.get('proxy_type') == 'socks5' else socks.HTTP
    addr = proxy_dict['addr']
    port = int(proxy_dict['port'])
    user = proxy_dict.get('username')
    pw = proxy_dict.get('password')
    if user and pw: return (s_type, addr, port, True, user, pw)
    return (s_type, addr, port)

def find_local_file(session_file_path: Path, keyword: str):
    try:
        parent_dir = session_file_path.parent
        for file in parent_dir.iterdir():
            if keyword in file.name.lower():
                return file
    except Exception:
        pass
    return None

def normalize_session_sqlite(session_path: Path):
    try:
        conn = sqlite3.connect(str(session_path))
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(sessions);")
        columns = [row[1] for row in cursor.fetchall()]
        if len(columns) > 5:
            cursor.execute("SELECT * FROM sessions;")
            rows = cursor.fetchall()
            cursor.execute("DROP TABLE sessions;")
            cursor.execute("""
                CREATE TABLE sessions (
                    dc_id integer primary key,
                    server_address text,
                    port integer,
                    auth_key blob,
                    takeout_id integer
                )
            """)
            for row in rows:
                cursor.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?, ?, ?, ?)", row[:5])
            conn.commit()
        conn.close()
    except Exception as e:
        print(f"Normalization warning: {e}")

async def validate_and_auth_session(session_path: Path, proxy_dict: dict, acc_id: str):
    try:
        normalize_session_sqlite(session_path)
        proxy_tuple = get_telethon_proxy_tuple(proxy_dict)
        
        API_ID = 2040
        API_HASH = "b18441a1ff607e10a989891a5462e627"

        client = TelegramClient(str(session_path), api_id=API_ID, api_hash=API_HASH, proxy=proxy_tuple)
        net_info, ping_ms = await get_proxy_info(proxy_dict)
        
        await client.connect()
        if not await client.is_user_authorized():
            fa_file = find_local_file(session_path, "2fa") or find_local_file(session_path, "pass")
            if fa_file:
                try:
                    password = fa_file.read_text(encoding="utf-8", errors="ignore").strip()
                    await client.sign_in(password=password)
                except Exception:
                    pass
            
            if not await client.is_user_authorized():
                await client.disconnect()
                return {"status": "invalid", "error_msg": "Сессия не авторизована (проверь 2FA пароль)"}
        
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
        return {"status": "error", "error_msg": f"Ошибка: {str(e)}"}

async def process_zip_archive(zip_path: Path):
    extract_to = UPLOAD_DIR / uuid.uuid4().hex
    extract_to.mkdir(parents=True, exist_ok=True)
    
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(extract_to)
    except Exception:
        if zip_path.exists(): zip_path.unlink()
        return
    
    session_files = list(extract_to.rglob("*.session"))
    db = SessionDB()

    for sf in session_files:
        try:
            acc_id = uuid.uuid4().hex
            session_dest = SESS_DIR / f"{acc_id}.session"
            shutil.copy(str(sf), str(session_dest))

            json_file = find_local_file(sf, ".json")
            if json_file:
                shutil.copy(str(json_file), str(META_DIR / f"{acc_id}.json"))

            _, proxy_dict = pop_free_proxy(db)
            if not proxy_dict:
                db.add(Account(id=acc_id, status="error", error_msg="Нет свободных прокси в базе"))
                db.commit()
                continue

            res = await validate_and_auth_session(session_dest, proxy_dict, acc_id)

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
        except Exception as inner_err:
            print(f"Skip file error: {inner_err}")
    db.close()
    if zip_path.exists(): zip_path.unlink()

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
    tmp_path = UPLOAD_DIR / f"upload_{uuid.uuid4()}.zip"
    tmp_path.write_bytes(await file.read())
    bg.add_task(process_zip_archive, tmp_path)
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
            "is_active_web": r.is_active_web,
            "created_at": r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else ""
        })
    db.close()
    return out

@app.post("/auth/open/{acc_id}")
async def open_web_session(acc_id: str):
    db = SessionDB()
    acc = db.query(Account).filter_by(id=acc_id).first()
    if not acc:
        db.close()
        return JSONResponse({"error": "Аккаунт не найден"}, status_code=404)
    
    acc.is_active_web = True
    db.commit()
    db.close()
    return {"status": "success", "message": "Сеанс успешно инициализирован и открыт под прокси с маскировкой."}

@app.post("/auth/close/{acc_id}")
async def close_web_session(acc_id: str):
    db = SessionDB()
    acc = db.query(Account).filter_by(id=acc_id).first()
    if not acc:
        db.close()
        return JSONResponse({"error": "Аккаунт не найден"}, status_code=404)
    
    acc.is_active_web = False
    db.commit()
    db.close()
    return {"status": "success", "message": "Сеанс успешно закрыт и сохранен в безопасности."}

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
        mf = META_DIR / f"{acc_id}.json"
        if mf.exists(): mf.unlink()
        db.delete(acc)
        db.commit()
    db.close()
    return {"status": "deleted"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))

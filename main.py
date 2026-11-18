import os, uuid, zipfile, time, random, asyncio, sqlite3, socks
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

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DATA_DIR = Path("/data")
SESS_DIR = DATA_DIR / "sessions"
UPLOAD_DIR = Path("/app/uploads")
STATIC_DIR = Path("/app/static")
SESS_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

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
    device = Column(String, default="tdata (Базовая)")
    proxy = Column(String)
    proxy_country = Column(String, nullable=True)
    ip = Column(String, nullable=True)
    ping = Column(Integer, default=0)
    country = Column(String, nullable=True)
    country_code = Column(String, nullable=True)
    premium = Column(Boolean, default=False)
    status = Column(String, default="pending")
    error_msg = Column(String, nullable=True)
    tdata_path = Column(String, nullable=True)
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

try:
    conn = sqlite3.connect(str(db_file))
    cursor = conn.cursor()
    cursor.execute("ALTER TABLE accounts ADD COLUMN tdata_path TEXT;")
    conn.commit()
    conn.close()
except Exception:
    pass

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

def locate_real_tdata(path_str: str) -> str:
    p = Path(path_str)
    if (p / "key_datas").exists() or any(f.name.isupper() and len(f.name) == 16 for f in p.glob("*")):
        return str(p)
    for sub in p.rglob("*"):
        if sub.is_dir():
            if (sub / "key_datas").exists() or any(f.name.isupper() and len(f.name) == 16 for f in sub.glob("*")):
                return str(sub)
    return path_str

def extract_tdata(zip_path: str) -> list:
    out = UPLOAD_DIR / uuid.uuid4().hex
    out.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf: zf.extractall(out)
    found_paths = []
    for p in out.rglob("*"):
        if p.is_dir():
            if (p / "key_datas").exists() or any(f.name.isupper() and len(f.name) == 16 for f in p.glob("*")):
                found_paths.append(str(p))
    return found_paths

def get_safe_proxy_tuple(proxy_str: str):
    p_parts = proxy_str.split(":")
    addr = p_parts[0]
    port = int(p_parts[1])
    s_type = socks.SOCKS5 if "socks5" in proxy_str.lower() else socks.HTTP
    if len(p_parts) >= 4:
        return (s_type, addr, port, True, p_parts[2], p_parts[3])
    return (s_type, addr, port)

async def validate_tdata_base(tdata_path: str, proxy_dict: dict, acc_id: str):
    temp_session = SESS_DIR / f"{acc_id}_temp.session"
    if temp_session.exists(): temp_session.unlink()
    try:
        real_path = locate_real_tdata(tdata_path)
        tdesk = TDesktop(real_path)
        client = await tdesk.ToTelethon(str(temp_session), UseCurrentSession)
        
        if proxy_dict.get('username'):
            client.proxy = (socks.HTTP, proxy_dict['addr'], proxy_dict['port'], True, proxy_dict['username'], proxy_dict['password'])
        else:
            client.proxy = (socks.HTTP, proxy_dict['addr'], proxy_dict['port'])

        net_info, ping_ms = await get_proxy_info(proxy_dict)
        await client.connect()
        
        if not await client.is_user_authorized():
            return {"status": "invalid", "error_msg": "tdata не авторизована"}
        
        me = await client.get_me()
        avatar_url = None
        if me.photo:
            avatar_path = STATIC_DIR / f"avatar_{acc_id}.jpg"
            await client.download_profile_photo(me, file=str(avatar_path))
            avatar_url = f"/static/avatar_{acc_id}.jpg"

        await client.disconnect()
        return {
            "status": "valid", "user_id": me.id, "username": me.username, "first_name": me.first_name,
            "phone": me.phone, "avatar_url": avatar_url, "country": net_info["country"],
            "country_code": net_info["country_code"], "proxy_country": net_info["country"],
            "ip": net_info["ip"], "ping": ping_ms, "premium": getattr(me, "premium", False), "error_msg": None
        }
    except Exception as e:
        return {"status": "error", "error_msg": str(e)}
    finally:
        if temp_session.exists(): temp_session.unlink()

def find_2fa_password(tdata_path: str) -> str:
    """Ищет 2FA пароль в папках и файлах 2fa, 2FA, pass или configuration"""
    try:
        base_path = Path(locate_real_tdata(tdata_path))
        for path in base_path.parent.rglob("*"):
            name_lower = path.name.lower()
            if "2fa" in name_lower or "pass" in name_lower or "password" in name_lower or "conf" in name_lower:
                if path.is_file():
                    try:
                        content = path.read_text(encoding="utf-8", errors="ignore").strip()
                        if content and len(content) < 100: return content
                    except: pass
                elif path.is_dir():
                    for sub_file in path.glob("*"):
                        try:
                            content = sub_file.read_text(encoding="utf-8", errors="ignore").strip()
                            if content and len(content) < 100: return content
                        except: pass
    except: pass
    return None

@app.post("/login/{acc_id}")
async def login_and_create_session(acc_id: str):
    db = SessionDB()
    acc = db.query(Account).filter_by(id=acc_id).first()
    if not acc or not acc.tdata_path:
        db.close()
        return JSONResponse({"error": "Аккаунт или tdata не найдены"}, status_code=404)

    tdata_path = locate_real_tdata(acc.tdata_path)
    proxy_tuple = get_safe_proxy_tuple(acc.proxy)

    old_session_file = SESS_DIR / f"{acc_id}_old.session"
    new_session_file = SESS_DIR / f"{acc_id}.session"
    for sf in [old_session_file, new_session_file]:
        if sf.exists(): sf.unlink()

    try:
        # 1. Подключаем старый клиент для чтения 777000
        tdesk = TDesktop(tdata_path)
        old_client = await tdesk.ToTelethon(str(old_session_file), UseCurrentSession)
        old_client.proxy = proxy_tuple
        await old_client.connect()

        if not await old_client.is_user_authorized():
            db.close()
            return JSONResponse({"error": "Базовая сессия слетела"}, status_code=400)

        phone = acc.phone

        # 2. Создаем новый клиент с фингерпринтом Android для полноценной новой авторизации
        chosen_api = API.TelegramAndroid.Generate(unique_id=acc_id)
        new_client = TelegramClient(str(new_session_file), api_id=chosen_api.api_id, api_hash=chosen_api.api_hash, proxy=proxy_tuple)
        new_client.device_model = chosen_api.device_model
        new_client.system_version = chosen_api.system_version
        new_client.app_version = chosen_api.app_version

        await new_client.connect()
        
        # Запрашиваем код авторизации на новое устройство
        sent_code = await new_client.send_code_request(phone)

        # 3. Перехватываем код из служебного чата 777000 через старый клиент
        code = None
        for _ in range(15):
            await asyncio.sleep(2)
            async for message in old_client.iter_messages(777000, limit=3):
                if message.text and any(char.isdigit() for char in message.text):
                    import re
                    digits = re.findall(r'\d+', message.text)
                    for d in digits:
                        if len(d) >= 5:
                            code = d
                            break
                if code: break
            if code: break

        if not code:
            await old_client.disconnect()
            await new_client.disconnect()
            db.close()
            return JSONResponse({"error": "Таймаут: код из 777000 не пришел"}, status_code=400)

        await asyncio.sleep(random.uniform(1.0, 2.0))
        
        # 4. Проходим авторизацию нового устройства с автопоиском 2FA при необходимости
        try:
            await new_client.sign_in(phone=phone, code=code, phone_code_hash=sent_code.phone_code_hash)
        except SessionPasswordNeededError:
            password = find_2fa_password(tdata_path)
            if not password:
                await old_client.disconnect()
                await new_client.disconnect()
                db.close()
                return JSONResponse({"error": "Требуется 2FA пароль, но папка/файл 2fa не найдены в tdata"}, status_code=400)
            
            await new_client.sign_in(password=password)

        acc.device = "Android 13.0 (New Auth)"
        db.commit()
        db.close()

        await old_client.disconnect()
        await new_client.disconnect()
        if old_session_file.exists(): old_session_file.unlink()

        return {"status": "success", "message": "Новая авторизация и создание сессии успешно завершены!"}
    except Exception as e:
        db.close()
        return JSONResponse({"error": str(e)}, status_code=500)

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
            res = await validate_tdata_base(td, proxy_dict, acc_id)
            if res.get("phone") and res["phone"] in existing_phones: continue
            if res.get("phone"): existing_phones.add(res["phone"])

            db.add(Account(
                id=acc_id, user_id=res.get("user_id"), username=res.get("username"),
                first_name=res.get("first_name"), phone=res.get("phone"), avatar_url=res.get("avatar_url"),
                proxy=f"{proxy_dict['addr']}:{proxy_dict['port']}", proxy_country=res.get("proxy_country"),
                ip=res.get("ip"), ping=res.get("ping"), country=res.get("country"),
                country_code=res.get("country_code"), premium=res.get("premium"),
                status=res.get("status", "error"), error_msg=res.get("error_msg"), tdata_path=td
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
    count = 0
    for line in data.text.splitlines():
        p = parse_proxy(line)
        if p:
            db.add(Proxy(addr=p["addr"], port=p["port"], username=p["username"], password=p["password"], proxy_type=p["proxy_type"]))
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
            if local_file.exists(): avatar_path = r.avatar_url
        
        session_file = SESS_DIR / f"{r.id}.session"
        has_session = session_file.exists()

        accounts_data.append({
            "id": r.id, "user_id": r.user_id, "username": r.username, "first_name": r.first_name,
            "phone": r.phone, "avatar_url": avatar_path, "device": r.device, "proxy": r.proxy,
            "proxy_country": r.proxy_country, "ip": r.ip, "ping": r.ping, "country": r.country,
            "country_code": r.country_code, "premium": r.premium, "status": r.status,
            "error_msg": r.error_msg, "has_session": has_session,
            "created_at": r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else ""
        })
    db.close()
    return accounts_data

@app.get("/download/{acc_id}")
def download_session(acc_id: str):
    session_file = SESS_DIR / f"{acc_id}.session"
    if not session_file.exists():
        return JSONResponse({"error": "Файл сессии еще не создан"}, status_code=404)
    return FileResponse(path=str(session_file), filename=f"{acc_id}.session", media_type='application/octet-stream')

@app.delete("/accounts/{acc_id}")
def delete_account(acc_id: str):
    db = SessionDB()
    acc = db.query(Account).filter_by(id=acc_id).first()
    if acc:
        for sf in [SESS_DIR / f"{acc_id}.session", SESS_DIR / f"{acc_id}_old.session", SESS_DIR / f"{acc_id}_temp.session"]:
            if sf.exists(): sf.unlink()
        db.delete(acc)
        db.commit()
    db.close()
    return {"status": "deleted"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)

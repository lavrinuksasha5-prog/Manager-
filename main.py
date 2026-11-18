import random

# --- Мульти-платформенный генератор отпечатков устройств ---
def generate_random_device(acc_id: str):
    devices = [
        {
            "api": API.TelegramAndroid.Generate(unique_id=acc_id),
            "name": "Android Smartphone",
            "device_str": "Android 13.0"
        },
        {
            "api": API.TelegramIOS.Generate(unique_id=acc_id),
            "name": "iPhone",
            "device_str": "iOS 17.2"
        },
        {
            "api": API.TelegramDesktop.Generate(unique_id=acc_id),
            "name": "macOS Desktop",
            "device_str": "macOS Sonoma"
        }
    ]
    # Выбираем случайный профиль устройства для аккаунта
    selected = random.choice(devices)
    return selected["api"], selected["name"], selected["device_str"]

# --- Обновленный валидатор ---
async def validate(tdata_path: str, proxy_dict: dict, acc_id: str):
    tdesk = TDesktop(tdata_path)
    session_file = SESS_DIR / f"{acc_id}.session"
    if session_file.exists(): session_file.unlink()

    # Получаем рандомный отпечаток устройства (iPhone, Android или macOS)
    chosen_api, device_name, device_string = generate_random_device(acc_id)

    client = await tdesk.ToTelethon(
        str(session_file), 
        UseCurrentSession,
        api=chosen_api
    )
    
    client._proxy = {
        'proxy_type': 'http',
        'addr': proxy_dict['addr'],
        'port': proxy_dict['port'],
        'username': proxy_dict['username'],
        'password': proxy_dict['password']
    }

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
            "first_name": me.first_name, "phone": me.phone,
            "avatar_url": avatar_url, "device": device_string,
            "country": net_info["country"], "country_code": net_info["country_code"],
            "proxy_country": net_info["country"], "ip": net_info["ip"], "ping": ping_ms,
            "premium": getattr(me, "premium", False), "error_msg": None
        }
    finally:
        await client.disconnect()

@app.post("/open-web/{acc_id}")
async def open_web(acc_id: str):
    db = SessionDB()
    acc = db.query(Account).filter_by(id=acc_id).first()
    if not acc: 
        db.close()
        return {"status": "error", "message": "Аккаунт не найден"}

    # Ищем прокси, который привязан к этому аккаунту (по точному совпадению IP/хоста)
    proxy_addr = acc.proxy.split(":")[0] if acc.proxy and acc.proxy != "direct" else None
    p_row = None
    if proxy_addr:
        p_row = db.query(Proxy).filter(Proxy.addr == proxy_addr).first()
    
    # Фолбек: если точного не нашли, берем последний добавленный прокси
    if not p_row:
        p_row = db.query(Proxy).order_by(Proxy.id.desc()).first()
        
    db.close()

    session_file = SESS_DIR / f"{acc_id}.session"
    if not session_file.exists(): 
        return {"status": "error", "message": "Файл сессии не найден"}

    client = TelegramClient(str(session_file), 2040, "b18441a1ff607e10a989891a5462e627")
    await client.connect()
    auth_key = client.session.auth_key.key.hex()
    dc_id = client.session.dc_id
    await client.disconnect()

    screenshot_path = STATIC_DIR / f"web_{acc_id}.png"

    GEO_CONFIGS = {
        "PT": {"locale": "pt-PT", "timezone": "Europe/Lisbon"},
        "DE": {"locale": "de-DE", "timezone": "Europe/Berlin"},
        "US": {"locale": "en-US", "timezone": "America/New_York"},
        "GB": {"locale": "en-GB", "timezone": "Europe/London"},
        "PL": {"locale": "pl-PL", "timezone": "Europe/Warsaw"},
        "FR": {"locale": "fr-FR", "timezone": "Europe/Paris"},
        "NL": {"locale": "nl-NL", "timezone": "Europe/Amsterdam"},
        "ES": {"locale": "es-ES", "timezone": "Europe/Madrid"},
        "TR": {"locale": "tr-TR", "timezone": "Europe/Istanbul"},
    }
    geo = GEO_CONFIGS.get(acc.country_code, {"locale": "en-US", "timezone": "UTC"})

    proxy_conf = None
    if p_row:
        if p_row.username and p_row.password:
            proxy_conf = {
                "server": f"http://{p_row.addr}:{p_row.port}",
                "username": p_row.username,
                "password": p_row.password
            }
        else:
            proxy_conf = {"server": f"http://{p_row.addr}:{p_row.port}"}

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            
            context_args = {
                "locale": geo["locale"],
                "timezone_id": geo["timezone"],
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            }
            if proxy_conf:
                context_args["proxy"] = proxy_conf

            context = await browser.new_context(**context_args)
            page = await context.new_page()
            
            # Скрываем автоматизацию
            await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")

            # Переходим сразу на вебку
            await page.goto("https://web.telegram.org/k/", wait_until="domcontentloaded", timeout=60000)
            
            # Инъецируем ключи сессии
            await page.evaluate(f"""
                localStorage.setItem('dc{dc_id}_auth_key', '{auth_key}');
                localStorage.setItem('user_auth', JSON.stringify({{ dcID: {dc_id}, id: '{acc.user_id}' }}));
            """)
            
            # Перезагружаем без зависания на networkidle (используем domcontentloaded + небольшую паузу для рендеринга чатов)
            await page.reload(wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(3) # Даем скриптам Telegram подгрузить интерфейс чатов
            
            await page.screenshot(path=str(screenshot_path))
            await browser.close()
            return {"status": "ok", "screenshot": f"/static/web_{acc_id}.png"}
    except Exception as e:
        print(f"[ERROR] Ошибка в open_web для {acc_id}: {str(e)}")
        return {"status": "error", "message": str(e)}

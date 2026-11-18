# Берем готовый образ с Telegram Desktop и веб-интерфейсом
FROM kasmweb/telegram:1.18.0

USER root

# Устанавливаем Python и зависимости для нашего бэкенда
RUN apt-get update && apt-get install -y python3 python3-pip && \
    pip3 install fastapi uvicorn sqlalchemy telethon pysocks aiohttp python-multipart

# Копируем наш код
WORKDIR /app
COPY main.py .
COPY index.html .

# Открываем порт для нашего API и порт для VNC (веб-интерфейса)
EXPOSE 8000 6901

# Запускаем и наш API, и VNC-сервер (через супервизор или скрипт)
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port 8000 & /dockerstartup/vnc_startup.sh"]

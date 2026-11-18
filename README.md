# TWKS Manager

Backend для менеджера Telegram-аккаунтов.

## Деплой на Railway
1. Подключи репозиторий.
2. Добавь Volume на `/data`.
3. Railway сам соберёт Dockerfile.

## API
- `GET /` — healthcheck
- `POST /upload` — загрузить ZIP с tdata
- `GET /accounts` — список аккаунтов
- `DELETE /accounts/{id}` — удалить

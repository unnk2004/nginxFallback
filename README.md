# nginxFallback

Nginx fallback для Xray (VLESS+TLS) с сайтом **Skynet Sync** и лёгким бэкендом.
Стек: nginx + один Python-файл (только стандартная библиотека) + SQLite + systemd. Ничего ставить через pip/npm не нужно.

## Структура

```
nginxFallback/
├── setup.sh            # установка и обновление
├── web/
│   ├── index.html      # главная: вход / регистрация, шаги, список ESP32
│   └── dashboard.html  # кабинет: API-ключ, узлы, последние данные
├── server/
│   └── server.py       # API (http.server + sqlite3 + scrypt)
└── README.md
```

## Быстрый старт

```
bash <(curl -fsSL https://raw.githubusercontent.com/unnk2004/nginxFallback/main/setup.sh)
```

Из клона репозитория скрипт берёт локальные файлы, иначе скачивает их с GitHub. Повторный запуск обновляет сайт и бэкенд, база сохраняется.

## Что делает setup.sh

1. Ставит `nginx`, `python3`, `curl`, если их нет
2. Спрашивает домен, ждёт сертификаты в `/root/cert/<домен>/fullchain.pem` и `privkey.pem`
3. Кладёт сайт в `/var/www/xray-fallback`, бэкенд в `/opt/skynet-sync`
4. Создаёт `skynet-sync.service` (динамический пользователь, база `/var/lib/skynet-sync/skynet.db`)
5. Настраивает nginx: порт 80 → HTTPS, `127.0.0.1:8443` (TLS) отдаёт сайт, `/api/` проксируется на `127.0.0.1:8080`

## Xray

```
"fallbacks": [{ "dest": 8443 }]
```

## Переменные окружения

| Переменная   | По умолчанию               | Описание                           |
|--------------|----------------------------|------------------------------------|
| `DOMAIN`     | спрашивает                 | Домен (пропускает вопрос)          |
| `YES`        | нет                        | `1` пропускает подтверждение       |
| `CERT_DIR`   | `/root/cert/<домен>`       | Каталог с сертификатами            |
| `WEBROOT`    | `/var/www/xray-fallback`   | Каталог сайта                      |
| `NGINX_PORT` | `8443`                     | Порт nginx для fallback            |
| `API_PORT`   | `8080`                     | Локальный порт бэкенда             |
| `REPO_RAW`   | raw.githubusercontent.com… | Откуда скачивать файлы             |

## API

| Метод и путь             | Назначение                                                        |
|--------------------------|-------------------------------------------------------------------|
| `POST /api/auth/register`| `{email, password}`, создаёт аккаунт и сессию                     |
| `POST /api/auth/login`   | `{email, password}`, не более 5 неудачных попыток на email / 10 мин |
| `POST /api/auth/logout`  | завершает сессию                                                  |
| `GET /api/me`            | почта, наличие ключа, список узлов                                |
| `POST /api/key`          | выпускает новый API-ключ (показывается один раз, хранится хеш)    |
| `POST /api/telemetry`    | заголовок `X-API-Key`, JSON с полем `node` и любыми показаниями   |

Все POST-запросы требуют `Content-Type: application/json`. Лимит: 5 узлов на аккаунт.

Пример отправки показаний:

```
curl -X POST https://<домен>/api/telemetry \
  -H "X-API-Key: sk_..." -H "Content-Type: application/json" \
  -d '{"node":"kitchen","temp":22.4,"hum":41}'
```

## Обслуживание

```
journalctl -u skynet-sync -n 50      # логи бэкенда
systemctl restart skynet-sync        # перезапуск
```

База лежит в `/var/lib/private/skynet-sync/skynet.db` (путь `/var/lib/skynet-sync` ведёт туда символической ссылкой), копируйте её целиком вместе с файлами `-wal` и `-shm` или через `sqlite3 .backup`.

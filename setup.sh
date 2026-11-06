#!/bin/bash
# nginxFallback v2: nginx fallback для Xray + сайт Skynet Sync с лёгким бэкендом
# (Python stdlib + SQLite, systemd). Повторный запуск = обновление, база сохраняется.

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

REPO_RAW="${REPO_RAW:-https://raw.githubusercontent.com/unnk2004/nginxFallback/main}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"

echo -e "${GREEN}=== Nginx fallback для Xray + Skynet Sync ===${NC}\n"

if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}Скрипт должен запускаться с правами root${NC}"
    exit 1
fi

# Зависимости: nginx, python3, curl
MISSING=()
command -v nginx   &> /dev/null || MISSING+=(nginx)
command -v python3 &> /dev/null || MISSING+=(python3)
command -v curl    &> /dev/null || MISSING+=(curl)
if [ ${#MISSING[@]} -gt 0 ]; then
    echo -e "${YELLOW}Устанавливаем: ${MISSING[*]}${NC}"
    apt-get update -qq && apt-get install -y "${MISSING[@]}" || { echo -e "${RED}Не удалось установить пакеты${NC}"; exit 1; }
fi
if ! python3 -c "import hashlib; hashlib.scrypt(b'x', salt=b'x', n=2**14, r=8, p=1)" 2>/dev/null; then
    echo -e "${RED}Python без поддержки scrypt (нужен Python 3.6+ с OpenSSL 1.1+)${NC}"
    exit 1
fi
PYTHON="$(command -v python3)"

# Домен
DOMAIN="${DOMAIN:-}"
if [ -z "$DOMAIN" ]; then
    echo -e "${YELLOW}Введите домен (например, de.skynets.uk):${NC}"
    read -p "Домен: " DOMAIN
fi
if [ -z "$DOMAIN" ]; then
    echo -e "${RED}Домен не может быть пустым${NC}"
    exit 1
fi

# Параметры (можно задать через переменные окружения)
CERT_DIR="${CERT_DIR:-/root/cert/$DOMAIN}"
WEBROOT="${WEBROOT:-/var/www/xray-fallback}"
NGINX_PORT="${NGINX_PORT:-8443}"
API_PORT="${API_PORT:-8080}"
APP_DIR="/opt/skynet-sync"

echo -e "\n${YELLOW}Параметры установки:${NC}"
echo -e " Домен:         ${GREEN}$DOMAIN${NC}"
echo -e " Сертификаты:   ${GREEN}$CERT_DIR${NC}"
echo -e " Webroot:       ${GREEN}$WEBROOT${NC}"
echo -e " Nginx порт:    ${GREEN}$NGINX_PORT${NC}"
echo -e " Бэкенд (loc.): ${GREEN}127.0.0.1:$API_PORT${NC}\n"

if [ "$YES" != "1" ]; then
    read -p "Продолжить установку? (y/n): " CONFIRM
    if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
        echo -e "${RED}Установка отменена${NC}"
        exit 0
    fi
fi

if [ ! -f "$CERT_DIR/fullchain.pem" ] || [ ! -f "$CERT_DIR/privkey.pem" ]; then
    echo -e "${RED}Сертификаты не найдены в $CERT_DIR${NC}"
    echo -e "${YELLOW}Убедитесь что сертификаты существуют и путь правильный${NC}"
    exit 1
fi
echo -e "${GREEN}Сертификаты найдены${NC}\n"

# Файлы проекта: из локального клона, иначе скачиваем из репозитория
fetch() {
    if [ -f "$SRC/$1" ]; then
        install -m 644 "$SRC/$1" "$2"
    else
        curl -fsSL "$REPO_RAW/$1" -o "$2" && chmod 644 "$2" || { echo -e "${RED}Не удалось скачать $1${NC}"; exit 1; }
    fi
}

echo -e "${YELLOW}Установка сайта и бэкенда...${NC}"
mkdir -p "$WEBROOT" "$APP_DIR"
fetch web/index.html      "$WEBROOT/index.html"
fetch web/dashboard.html  "$WEBROOT/dashboard.html"
fetch server/server.py    "$APP_DIR/server.py"

# systemd: динамический пользователь, база в /var/lib/skynet-sync
cat > /etc/systemd/system/skynet-sync.service << EOF
[Unit]
Description=Skynet Sync API
After=network.target

[Service]
ExecStart=$PYTHON $APP_DIR/server.py
Environment=SKYNET_PORT=$API_PORT
Environment=SKYNET_DB=/var/lib/skynet-sync/skynet.db
DynamicUser=yes
StateDirectory=skynet-sync
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable skynet-sync > /dev/null 2>&1
systemctl restart skynet-sync
sleep 1
if ! systemctl is-active --quiet skynet-sync; then
    echo -e "${RED}Бэкенд не запустился. Смотрите: journalctl -u skynet-sync -n 30${NC}"
    exit 1
fi
echo -e "${GREEN}Бэкенд запущен${NC}\n"

# Nginx
echo -e "${YELLOW}Настройка Nginx...${NC}"
cat > /etc/nginx/sites-available/xray-fallback << EOF
# HTTP -> HTTPS redirect
server {
    listen 80;
    listen [::]:80;
    server_name _;
    return 301 https://\$host\$request_uri;
}

# HTTPS fallback для Xray
server {
    listen 127.0.0.1:$NGINX_PORT ssl http2;
    server_name _;
    server_tokens off;

    ssl_certificate $CERT_DIR/fullchain.pem;
    ssl_certificate_key $CERT_DIR/privkey.pem;

    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 10m;

    root $WEBROOT;
    index index.html;

    location / {
        try_files \$uri \$uri/ =404;
    }

    location = /dashboard {
        try_files /dashboard.html =404;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:$API_PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        client_max_body_size 16k;
    }

    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "same-origin" always;

    access_log /var/log/nginx/xray-fallback-access.log;
    error_log /var/log/nginx/xray-fallback-error.log;
}
EOF

ln -sf /etc/nginx/sites-available/xray-fallback /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default

if nginx -t; then
    systemctl enable nginx > /dev/null 2>&1
    systemctl restart nginx
    echo -e "\n${GREEN}Настройка завершена успешно!${NC}\n"
    echo -e "📍 Nginx:     ${GREEN}127.0.0.1:$NGINX_PORT${NC} (HTTPS), порт 80 → HTTPS"
    echo -e "📍 Сайт:      ${GREEN}$WEBROOT${NC}"
    echo -e "📍 Бэкенд:    ${GREEN}skynet-sync.service${NC}, база ${GREEN}/var/lib/skynet-sync/skynet.db${NC}"
    echo -e "📍 Логи:      journalctl -u skynet-sync, /var/log/nginx/xray-fallback-*"
    echo -e "\n${YELLOW}Xray fallback:${NC}"
    echo -e "${GREEN}  \"fallbacks\": [{ \"dest\": $NGINX_PORT }]${NC}"
    echo -e "\n${GREEN}Проверка:${NC} curl -I https://$DOMAIN"
else
    echo -e "${RED}Ошибка в конфигурации Nginx${NC}"
    exit 1
fi

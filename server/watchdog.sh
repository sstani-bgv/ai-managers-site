#!/bin/sh
# Раз в минуту (bugaev-watchdog.timer): если сервис не отвечает за 10 с — перезапуск и короткое сообщение в Telegram без личных данных.
# Появился после 12–16.10.2026: сервис заявок завис на 4 дня, и никто не узнал.
. /etc/bugaev-leads/env 2>/dev/null  # BOT_TOKEN, CHAT_ID

alert() {
  curl -s -m 10 -d chat_id="$CHAT_ID" --data-urlencode text="bugaev-ai.ru: $1 не отвечал — перезапущен" \
    "https://api.telegram.org/bot$BOT_TOKEN/sendMessage" >/dev/null
}

curl -fsS -m 10 -o /dev/null 'http://127.0.0.1:8081/api/demo/hello?n=general' || { systemctl restart bugaev-leads; alert 'сервис заявок и чата'; }
curl -fsS -m 10 -o /dev/null 'http://127.0.0.1:8082/spend' || { systemctl restart bugaev-voice; alert 'голосовой сервис'; }

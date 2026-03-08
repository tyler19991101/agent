#!/bin/bash

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_PATH="$PROJECT_DIR/bot.py"
ENV_FILE="$PROJECT_DIR/.env.bot"
LOG_DIR="$PROJECT_DIR/logs"
BOT_LOG_PATH="$LOG_DIR/bot.log"
NGROK_LOG_PATH="$LOG_DIR/ngrok.log"
MY_DOMAIN="acaulescent-daxton-semiarid.ngrok-free.dev"
SESSION_TS="$(date '+%Y%m%d_%H%M%S')"

if [ ! -f "$ENV_FILE" ]; then
  echo "缺少 $ENV_FILE"
  echo "請先建立 .env.bot，至少填入 LINE_CHANNEL_ACCESS_TOKEN、LINE_CHANNEL_SECRET、DIFY_API_KEY。"
  exit 1
fi

mkdir -p "$LOG_DIR"

rotate_log() {
  local log_path="$1"
  local stem="$2"
  if [ -f "$log_path" ] && [ -s "$log_path" ]; then
    mv "$log_path" "$LOG_DIR/${stem}_${SESSION_TS}.log"
  fi
}

rotate_log "$BOT_LOG_PATH" "bot"
rotate_log "$NGROK_LOG_PATH" "ngrok"

osascript -e "tell application \"Terminal\" to do script \"export PUBLIC_BASE_URL='https://$MY_DOMAIN'; export BOT_LOG_PATH='$BOT_LOG_PATH'; set -a; source '$ENV_FILE'; set +a; python3 '$BOT_PATH'\""
osascript -e "tell application \"Terminal\" to do script \"ngrok http --url=$MY_DOMAIN 8080 > '$NGROK_LOG_PATH' 2>&1\""

echo "已嘗試啟動 LINE 秘書代理與 ngrok。"
echo "Bot log: $BOT_LOG_PATH"
echo "ngrok log: $NGROK_LOG_PATH"

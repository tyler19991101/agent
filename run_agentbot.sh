#!/bin/bash

BOT_PATH="/Users/linxuanli/Library/Mobile Documents/com~apple~CloudDocs/Code/bot.py"
ENV_FILE="/Users/linxuanli/Library/Mobile Documents/com~apple~CloudDocs/Code/.env.bot"
MY_DOMAIN="acaulescent-daxton-semiarid.ngrok-free.dev"

if [ ! -f "$ENV_FILE" ]; then
  echo "缺少 $ENV_FILE"
  echo "請先建立 .env.bot，至少填入 LINE_CHANNEL_ACCESS_TOKEN、LINE_CHANNEL_SECRET、DIFY_API_KEY。"
  exit 1
fi

osascript -e "tell application \"Terminal\" to do script \"set -a; source '$ENV_FILE'; set +a; python3 '$BOT_PATH'\""
osascript -e "tell application \"Terminal\" to do script \"ngrok http --url=$MY_DOMAIN 8080\""

echo "已嘗試啟動 LINE 秘書代理與 ngrok。"
echo "請確認 bot 視窗內環境變數與執行狀態是否正常。"

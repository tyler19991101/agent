import os

from flask import Flask, abort, request
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import AudioMessageContent, MessageEvent, TextMessageContent

from secretary_agent.config import Settings
from secretary_agent.memory import SQLiteStore
from secretary_agent.runtime import SecretaryRuntime
from secretary_agent.transport_line import LineMessenger, normalize_line_message


settings = Settings.from_env()
store = SQLiteStore(settings.database_path)
messenger = LineMessenger(settings.line_channel_access_token, store)
runtime = SecretaryRuntime(settings=settings, store=store, messenger=messenger)
runtime.start()

app = Flask(__name__)
handler = WebhookHandler(settings.line_channel_secret)


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    except Exception:
        abort(500)
    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def on_message(event: MessageEvent):
    inbound = normalize_line_message(event)
    runtime.handle_inbound_message(inbound)


@handler.add(MessageEvent, message=AudioMessageContent)
def on_audio_message(event: MessageEvent):
    try:
        audio_bytes = messenger.get_message_content(event.message.id)
        transcript = runtime.agent_client.audio_to_text(
            memory_key=f"user:{getattr(event.source, 'user_id', None) or getattr(event.source, 'group_id', None) or getattr(event.source, 'room_id', None) or 'anonymous'}",
            audio_bytes=audio_bytes,
            filename=f"{event.message.id}.m4a",
            mime_type="audio/m4a",
        )
    except Exception as err:
        messenger.reply_text(event.reply_token, f"語音轉文字失敗：{err}")
        return

    if not transcript:
        messenger.reply_text(event.reply_token, "語音轉文字失敗，請再試一次或改用文字輸入。")
        return

    inbound = normalize_line_message(event, text_override=transcript)
    runtime.handle_inbound_message(inbound)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)

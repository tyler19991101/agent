import logging
import os
import threading

from flask import Flask, abort, request
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import (
    AudioMessageContent,
    FileMessageContent,
    LocationMessageContent,
    MessageEvent,
    TextMessageContent,
)

from secretary_agent.audio_transcriber import AssemblyAIAudioTranscriber, AudioTranscriptionError
from secretary_agent.config import Settings
from secretary_agent.memory import SQLiteStore
from secretary_agent.runtime import SecretaryRuntime
from secretary_agent.transport_line import (
    LineMessenger,
    format_location_message,
    get_push_target_id,
    infer_media_metadata,
    normalize_line_message,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("lineagent.bot")


settings = Settings.from_env()
store = SQLiteStore(settings.database_path)
messenger = LineMessenger(settings.line_channel_access_token, store)
runtime = SecretaryRuntime(settings=settings, store=store, messenger=messenger)
try:
    audio_transcriber = (
        AssemblyAIAudioTranscriber.from_settings(settings)
        if settings.stt_provider == "assemblyai"
        else None
    )
except AudioTranscriptionError:
    audio_transcriber = None
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
        logger.exception("Unhandled exception while processing LINE callback")
        abort(500)
    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def on_message(event: MessageEvent):
    inbound = normalize_line_message(event)
    runtime.handle_inbound_message(inbound)


@handler.add(MessageEvent, message=AudioMessageContent)
def on_audio_message(event: MessageEvent):
    _start_media_processing(event)


@handler.add(MessageEvent, message=FileMessageContent)
def on_file_message(event: MessageEvent):
    _start_media_processing(event)


def _start_media_processing(event: MessageEvent):
    if audio_transcriber is None:
        messenger.reply_text(event.reply_token, "目前未啟用語音逐字稿服務，請改用文字輸入。")
        return
    messenger.reply_text(
        event.reply_token,
        "已收到語音，正在轉錄並整理重點，完成後我會主動推送給你。",
    )
    threading.Thread(target=_handle_media_message, args=(event,), daemon=True).start()


def _handle_media_message(event: MessageEvent):
    push_target_id = get_push_target_id(event.source)
    if not push_target_id:
        logger.error("Missing push target for media message id=%s", getattr(event.message, "id", ""))
        return
    if audio_transcriber is None:
        messenger.push_text(push_target_id, "目前未啟用語音逐字稿服務，請改用文字輸入。")
        return
    try:
        filename, mime_type = infer_media_metadata(event.message)
        logger.info(
            "Processing media message id=%s type=%s filename=%s mime=%s",
            getattr(event.message, "id", ""),
            getattr(event.message, "type", ""),
            filename,
            mime_type,
        )
        if not mime_type.startswith("audio/") and mime_type != "application/octet-stream":
            messenger.push_text(
                push_target_id,
                "這個檔案不是可辨識的音訊格式，請改傳 LINE 語音、mp3、m4a 或 wav。",
            )
            return
        audio_bytes = messenger.get_message_content(
            event.message.id,
            message_type=getattr(event.message, "type", ""),
        )
        logger.info("Downloaded media bytes=%s for message id=%s", len(audio_bytes), getattr(event.message, "id", ""))
        transcript = audio_transcriber.transcribe_to_prompt(
            audio_bytes=audio_bytes,
            filename=filename or f"{event.message.id}.bin",
            mime_type=mime_type,
        )
    except AudioTranscriptionError as err:
        logger.warning("Audio transcription failed: %s", err)
        messenger.push_text(push_target_id, f"語音轉文字失敗：{err}")
        return
    except Exception as err:
        logger.exception("Unexpected media processing failure")
        messenger.push_text(
            push_target_id,
            f"語音處理失敗：{err.__class__.__name__}: {str(err)[:180]}",
        )
        return

    if not transcript:
        messenger.push_text(push_target_id, "語音轉文字失敗，請再試一次或改用文字輸入。")
        return

    inbound = normalize_line_message(event, text_override=transcript, reply_enabled=False)
    runtime.handle_inbound_message(inbound)


@handler.add(MessageEvent, message=LocationMessageContent)
def on_location_message(event: MessageEvent):
    inbound = normalize_line_message(event, text_override=format_location_message(event.message))
    runtime.handle_inbound_message(inbound)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)

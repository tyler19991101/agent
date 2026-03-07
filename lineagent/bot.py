import logging
import os
import threading

from flask import Flask, abort, request, send_file
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
from secretary_agent.logging_utils import format_log_event
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
USER_SAFE_SYSTEM_ERROR_TEXT = "系統有錯誤，已通知 IT 處理，請稍後再試。"


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
        logger.exception(format_log_event("callback_unhandled_exception"))
        abort(500)
    return "OK"


@app.route("/downloads/<token>", methods=["GET"])
def download_artifact(token: str):
    row = store.get_artifact_by_ref_key(token)
    if not row:
        abort(404)
    import json

    metadata = json.loads(row["metadata_json"] or "{}")
    path = metadata.get("path", "")
    filename = metadata.get("filename") or row["content"]
    if not path or not os.path.isfile(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=filename)


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
    message_id = getattr(event.message, "id", "")
    source_id = get_push_target_id(event.source)
    user_id = getattr(event.source, "user_id", None)
    if audio_transcriber is None:
        logger.error(
            format_log_event(
                "media_transcriber_missing",
                message_id=message_id,
                source_id=source_id,
                user_id=user_id,
            )
        )
        messenger.reply_text(event.reply_token, USER_SAFE_SYSTEM_ERROR_TEXT)
        return
    logger.info(
        format_log_event(
            "media_ack_sent",
            message_id=message_id,
            message_type=getattr(event.message, "type", ""),
            source_id=source_id,
            user_id=user_id,
        )
    )
    messenger.reply_text(
        event.reply_token,
        "已收到語音，正在轉錄並整理重點，完成後我會主動推送給你。",
    )
    threading.Thread(target=_handle_media_message, args=(event,), daemon=True).start()


def _handle_media_message(event: MessageEvent):
    push_target_id = get_push_target_id(event.source)
    message_id = getattr(event.message, "id", "")
    source_id = push_target_id
    user_id = getattr(event.source, "user_id", None)
    if not push_target_id:
        logger.error(
            format_log_event(
                "media_missing_push_target",
                message_id=message_id,
                user_id=user_id,
            )
        )
        return
    if audio_transcriber is None:
        logger.error(
            format_log_event(
                "media_transcriber_missing_runtime",
                message_id=message_id,
                source_id=source_id,
                user_id=user_id,
            )
        )
        messenger.push_text(push_target_id, USER_SAFE_SYSTEM_ERROR_TEXT)
        return
    try:
        filename, mime_type = infer_media_metadata(event.message)
        logger.info(
            format_log_event(
                "media_processing_started",
                message_id=message_id,
                message_type=getattr(event.message, "type", ""),
                filename=filename,
                mime=mime_type,
                source_id=source_id,
                user_id=user_id,
            )
        )
        if not mime_type.startswith("audio/") and mime_type != "application/octet-stream":
            logger.warning(
                format_log_event(
                    "media_unsupported_format",
                    message_id=message_id,
                    filename=filename,
                    mime=mime_type,
                    source_id=source_id,
                    user_id=user_id,
                )
            )
            messenger.push_text(
                push_target_id,
                "這個檔案不是可辨識的音訊格式，請改傳 LINE 語音、mp3、m4a 或 wav。",
            )
            return
        audio_bytes = messenger.get_message_content(
            event.message.id,
            message_type=getattr(event.message, "type", ""),
        )
        logger.info(
            format_log_event(
                "media_downloaded",
                message_id=message_id,
                bytes=len(audio_bytes),
                source_id=source_id,
                user_id=user_id,
            )
        )
        transcript = audio_transcriber.transcribe_to_prompt(
            audio_bytes=audio_bytes,
            filename=filename or f"{event.message.id}.bin",
            mime_type=mime_type,
        )
        logger.info(
            format_log_event(
                "media_transcription_completed",
                message_id=message_id,
                transcript_chars=len(transcript or ""),
                source_id=source_id,
                user_id=user_id,
            )
        )
    except AudioTranscriptionError as err:
        logger.exception(
            format_log_event(
                "media_transcription_failed",
                message_id=message_id,
                source_id=source_id,
                user_id=user_id,
                error_type=type(err).__name__,
            )
        )
        messenger.push_text(push_target_id, USER_SAFE_SYSTEM_ERROR_TEXT)
        return
    except Exception as err:
        logger.exception(
            format_log_event(
                "media_processing_failed",
                message_id=message_id,
                source_id=source_id,
                user_id=user_id,
                error_type=type(err).__name__,
            )
        )
        messenger.push_text(push_target_id, USER_SAFE_SYSTEM_ERROR_TEXT)
        return

    if not transcript:
        logger.error(
            format_log_event(
                "media_transcription_empty",
                message_id=message_id,
                source_id=source_id,
                user_id=user_id,
            )
        )
        messenger.push_text(push_target_id, USER_SAFE_SYSTEM_ERROR_TEXT)
        return

    inbound = normalize_line_message(event, text_override=transcript, reply_enabled=False)
    logger.info(
        format_log_event(
            "media_runtime_dispatch",
            message_id=message_id,
            memory_key=inbound.memory_key,
            source_id=source_id,
            user_id=user_id,
        )
    )
    runtime.handle_inbound_message(inbound)


@handler.add(MessageEvent, message=LocationMessageContent)
def on_location_message(event: MessageEvent):
    inbound = normalize_line_message(event, text_override=format_location_message(event.message))
    runtime.handle_inbound_message(inbound)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)

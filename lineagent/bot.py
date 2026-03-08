import logging
import os
import threading

from flask import Flask, abort, redirect, request, send_file
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

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(PROJECT_DIR, "logs")
LOG_PATH = os.getenv("BOT_LOG_PATH", os.path.join(LOG_DIR, "bot.log"))
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
    ],
    force=True,
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


@app.route("/auth/google/start", methods=["GET"])
def google_auth_start():
    state_token = request.args.get("state", "").strip()
    row = store.get_oauth_state(state_token)
    if not row or row["status"] != "pending":
        abort(404)
    if not runtime.google_client.is_configured:
        abort(503)
    return redirect(runtime.google_client.build_auth_url(state_token))


@app.route("/auth/google/callback", methods=["GET"])
def google_auth_callback():
    state_token = request.args.get("state", "").strip()
    code = request.args.get("code", "").strip()
    row = store.get_oauth_state(state_token)
    if not row or row["status"] != "pending":
        abort(404)
    if not code:
        abort(400)
    try:
        token_payload = runtime.google_client.exchange_code(code)
        store.upsert_connected_account(
            row["memory_key"],
            service_name="google",
            login_identifier="google-linked-account",
            display_name="Google",
            oauth_provider="google",
            session_available=True,
            metadata=token_payload,
        )
        store.log_memory_change(
            row["memory_key"],
            "google_account_connected",
            {"account_label": "Google"},
        )
        store.resolve_oauth_state(state_token)
        if row["run_id"]:
            store.update_run_status(
                int(row["run_id"]),
                status="queued",
                current_phase="google_auth_resolved",
                requires_approval=False,
            )
        messenger.push_text(
            runtime._memory_key_to_push_target(row["memory_key"]),
            "Google 綁定成功，我會繼續處理你的行程或提醒需求。",
        )
        return (
            "<h1>Google 綁定成功</h1>"
            "<p>你可以回到 LINE，我會繼續處理剛剛的任務。</p>"
        )
    except Exception as err:
        logger.exception(format_log_event("google_auth_callback_failed", error_type=type(err).__name__))
        messenger.push_text(
            runtime._memory_key_to_push_target(row["memory_key"]),
            USER_SAFE_SYSTEM_ERROR_TEXT,
        )
        return (
            "<h1>Google 綁定失敗</h1>"
            "<p>系統已記錄錯誤，請回到 LINE 稍後再試。</p>"
        ), 500


@app.route("/automation/<token>", methods=["GET", "POST"])
def automation_review(token: str):
    checkpoint = store.get_sensitive_checkpoint(token)
    if not checkpoint:
        abort(404)
    context = runtime.browser_automation.build_review_page_context(checkpoint)
    if request.method == "POST":
        action = request.form.get("action", "").strip().lower()
        if action not in {"approve", "cancel"}:
            abort(400)
        store.resolve_sensitive_checkpoint(token, "approved" if action == "approve" else "cancelled")
        automation = context["automation"]
        if automation:
            store.update_automation_run(
                int(automation["id"]),
                status="approved" if action == "approve" else "cancelled",
                result_payload={"action": action},
            )
        target_id = runtime._memory_key_to_push_target(checkpoint["memory_key"])
        if action == "cancel":
            store.update_run_status(
                int(checkpoint["run_id"]),
                status="failed",
                current_phase="cancelled",
                error="User cancelled automation review",
                finished=True,
            )
            messenger.push_text(target_id, "已取消這次自動操作任務。")
            return "<h1>已取消</h1><p>這次自動操作任務已取消。</p>"

        store.update_run_status(
            int(checkpoint["run_id"]),
            status="failed",
            current_phase="automation_reviewed",
            error="Browser automation worker is not enabled in this environment",
            finished=True,
        )
        messenger.push_text(
            target_id,
            "已收到你的確認。這個環境目前尚未啟用瀏覽器自動操作 worker，因此我先保留這次操作需求。",
        )
        return (
            "<h1>已收到確認</h1>"
            "<p>目前這個環境尚未啟用瀏覽器自動操作 worker，系統已保留這次操作需求。</p>"
        )

    request_payload = context["request_payload"]
    browser_request = request_payload.get("browser_request", {})
    profile_snapshot = request_payload.get("profile_snapshot", {})
    target_items = browser_request.get("target_items", []) or []
    fields_needed = browser_request.get("user_profile_fields_needed", []) or []
    return f"""
    <html>
      <head><meta charset="utf-8"><title>自動操作確認</title></head>
      <body style="font-family: -apple-system, sans-serif; max-width: 760px; margin: 32px auto; line-height: 1.6;">
        <h1>自動操作確認</h1>
        <p>網站：{browser_request.get("domain", "未指定")}</p>
        <p>目標：{browser_request.get("intent", "未指定")}</p>
        <p>項目：{", ".join(target_items) if target_items else "未指定"}</p>
        <p>將使用的個人欄位：{", ".join(fields_needed) if fields_needed else "未指定"}</p>
        <pre style="background:#f5f5f5;padding:16px;border-radius:8px;overflow:auto;">{profile_snapshot}</pre>
        <form method="post" style="display:flex;gap:12px;">
          <button type="submit" name="action" value="approve">確認</button>
          <button type="submit" name="action" value="cancel">取消</button>
        </form>
        <p style="color:#666;">目前 v1 會先建立確認流程與資料檢查，正式瀏覽器自動執行 worker 需另外啟用。</p>
      </body>
    </html>
    """


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

import mimetypes
import os
import time
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

from linebot.v3.messaging import (
    ApiClient,
    Configuration,
    MessagingApi,
    MessagingApiBlob,
    PushMessageRequest,
    ReplyMessageRequest,
    TextMessage,
)

from secretary_agent.models import InboundMessage
from secretary_agent.utils import split_text


def get_push_target_id(event_source: Any) -> Optional[str]:
    for key in ("user_id", "group_id", "room_id"):
        value = getattr(event_source, key, None)
        if value:
            return value
    return None


def get_quoted_message_id(message: Any) -> Optional[str]:
    quoted_id = getattr(message, "quoted_message_id", None)
    if quoted_id:
        return quoted_id
    return getattr(message, "quotedMessageId", None)


def get_line_event_id(event: Any) -> Optional[str]:
    event_id = getattr(event, "webhook_event_id", None)
    if event_id:
        return event_id
    return getattr(event, "webhookEventId", None)


def normalize_line_message(
    event: Any,
    text_override: Optional[str] = None,
    *,
    reply_enabled: bool = True,
    image_asset_ids: Optional[List[int]] = None,
) -> InboundMessage:
    source_id = get_push_target_id(event.source) or "anonymous"
    user_id = getattr(event.source, "user_id", None)
    source_type = getattr(event.source, "type", "unknown")
    return InboundMessage(
        source_type=source_type,
        source_id=source_id,
        user_id=user_id,
        reply_token=event.reply_token,
        reply_enabled=reply_enabled,
        text=(text_override if text_override is not None else (event.message.text or "")).strip(),
        image_asset_ids=list(image_asset_ids or []),
        quoted_message_id=get_quoted_message_id(event.message),
        received_at=datetime.now(timezone.utc),
        line_event_id=get_line_event_id(event),
    )


def format_location_message(message: Any) -> str:
    title = (getattr(message, "title", "") or "使用者位置").strip()
    address = (getattr(message, "address", "") or "").strip()
    latitude = getattr(message, "latitude", None)
    longitude = getattr(message, "longitude", None)

    lines = [
        "使用者傳送了 LINE 位置訊息，請優先依照這個位置資訊處理附近查詢需求。",
        f"位置名稱：{title}",
    ]
    if address:
        lines.append(f"地址：{address}")
    if latitude is not None and longitude is not None:
        lines.append(f"座標：{latitude}, {longitude}")
    lines.append("若任務涉及附近餐廳、景點、店家、診所、咖啡廳或在地推薦，請把這個位置當成主要查詢依據。")
    return "\n".join(lines)


def infer_media_metadata(message: Any) -> Tuple[str, str]:
    original_name = (getattr(message, "file_name", None) or getattr(message, "fileName", None) or "").strip()
    extension = os.path.splitext(original_name)[1].lower()

    if getattr(message, "type", "") == "audio":
        if extension == ".mp3":
            return original_name or "audio.mp3", "audio/mpeg"
        if extension in {".wav", ".wave"}:
            return original_name or "audio.wav", "audio/wav"
        if extension == ".ogg":
            return original_name or "audio.ogg", "audio/ogg"
        if extension == ".aac":
            return original_name or "audio.aac", "audio/aac"
        return original_name or "audio.m4a", "audio/m4a"

    guessed = mimetypes.guess_type(original_name)[0] if original_name else None
    if guessed and guessed.startswith("audio/"):
        return original_name, guessed

    if extension == ".mp3":
        return original_name or "audio.mp3", "audio/mpeg"
    if extension in {".m4a", ".mp4"}:
        return original_name or "audio.m4a", "audio/m4a"
    if extension in {".wav", ".wave"}:
        return original_name or "audio.wav", "audio/wav"
    if extension == ".ogg":
        return original_name or "audio.ogg", "audio/ogg"

    return original_name or "audio.bin", guessed or "application/octet-stream"


def extract_sent_message_ids(api_response: Any) -> List[str]:
    sent_ids: List[str] = []
    if api_response is None:
        return sent_ids
    sent_messages = getattr(api_response, "sent_messages", None)
    if sent_messages:
        for msg in sent_messages:
            message_id = getattr(msg, "id", None)
            if message_id:
                sent_ids.append(message_id)
    return sent_ids


class LineMessenger:
    CONTENT_READY_TIMEOUT_SECONDS = 20.0
    CONTENT_READY_POLL_SECONDS = 1.0

    def __init__(self, access_token: str, store: Any):
        self.configuration = Configuration(access_token=access_token)
        self.store = store

    def split_for_storage(self, text: str) -> List[str]:
        return split_text(text, 4300)

    def reply_text(self, reply_token: str, text: str) -> List[str]:
        with ApiClient(self.configuration) as api_client:
            api = MessagingApi(api_client)
            response = api.reply_message(
                ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(text=text[:4500])],
                )
            )
        return extract_sent_message_ids(response)

    def push_text(self, target_id: str, text: str) -> List[str]:
        sent_ids: List[str] = []
        with ApiClient(self.configuration) as api_client:
            api = MessagingApi(api_client)
            for chunk in split_text(text, 4300):
                response = api.push_message(
                    PushMessageRequest(to=target_id, messages=[TextMessage(text=chunk)])
                )
                sent_ids.extend(extract_sent_message_ids(response))
        return sent_ids

    def get_message_content(self, message_id: str, message_type: str = "") -> bytes:
        with ApiClient(self.configuration) as api_client:
            api = MessagingApiBlob(api_client)
            normalized_type = (message_type or "").lower()
            if normalized_type in {"audio", "video"}:
                self._wait_for_media_content_ready(api, message_id)
            response = api.get_message_content(message_id)
            if isinstance(response, (bytes, bytearray)):
                data = bytes(response)
                if data:
                    return data

            response_with_info = api.get_message_content_with_http_info(
                message_id,
                _preload_content=False,
            )
            raw = getattr(response_with_info, "raw_data", None)
            if hasattr(raw, "read"):
                data = raw.read()
                if data:
                    return data
            if isinstance(raw, (bytes, bytearray)):
                data = bytes(raw)
                if data:
                    return data
        raise ValueError(f"LINE message content is empty for message_id={message_id}")

    def _wait_for_media_content_ready(self, api: MessagingApiBlob, message_id: str) -> None:
        deadline = time.time() + self.CONTENT_READY_TIMEOUT_SECONDS
        last_status = ""
        while time.time() < deadline:
            status_resp = api.get_message_content_transcoding_by_message_id(message_id)
            status = str(getattr(status_resp, "status", "")).lower()
            last_status = status
            if status in {"succeeded", "success"}:
                return
            if status in {"failed", "error"}:
                raise ValueError(f"LINE media transcoding failed for message_id={message_id}")
            time.sleep(self.CONTENT_READY_POLL_SECONDS)
        raise ValueError(
            f"LINE media content not ready for message_id={message_id} status={last_status or 'unknown'}"
        )

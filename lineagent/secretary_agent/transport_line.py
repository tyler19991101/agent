from datetime import datetime, timezone
from typing import Any, List, Optional

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


def normalize_line_message(event: Any, text_override: Optional[str] = None) -> InboundMessage:
    source_id = get_push_target_id(event.source) or "anonymous"
    user_id = getattr(event.source, "user_id", None)
    source_type = getattr(event.source, "type", "unknown")
    return InboundMessage(
        source_type=source_type,
        source_id=source_id,
        user_id=user_id,
        reply_token=event.reply_token,
        text=(text_override if text_override is not None else (event.message.text or "")).strip(),
        quoted_message_id=get_quoted_message_id(event.message),
        received_at=datetime.now(timezone.utc),
        line_event_id=get_line_event_id(event),
    )


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

    def get_message_content(self, message_id: str) -> bytes:
        with ApiClient(self.configuration) as api_client:
            api = MessagingApiBlob(api_client)
            response = api.get_message_content(message_id)
            if hasattr(response, "read"):
                return response.read()
            if isinstance(response, bytes):
                return response
            return bytes(response)

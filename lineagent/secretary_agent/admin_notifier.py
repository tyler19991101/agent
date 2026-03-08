from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict

from secretary_agent.logging_utils import format_log_event


class AdminNotifier:
    def __init__(self, *, messenger: Any, admin_line_user_id: str, logger: logging.Logger):
        self.messenger = messenger
        self.admin_line_user_id = admin_line_user_id.strip()
        self.logger = logger

    def notify_system_error(self, *, event: str, summary: str, fields: Dict[str, Any] | None = None) -> None:
        if not self.admin_line_user_id:
            return
        payload = fields or {}
        lines = [
            "LINE 助理系統異常通知",
            f"時間：{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}",
            f"事件：{event}",
            f"摘要：{summary}",
        ]
        for key in ("run_id", "memory_key", "source_id", "user_id", "message_id", "line_event_id", "error_type"):
            value = payload.get(key)
            if value:
                lines.append(f"{key}：{value}")
        lines.append("請查看 bot.log 進一步排查。")
        text = "\n".join(lines)
        try:
            self.messenger.push_text(self.admin_line_user_id, text)
            self.logger.info(
                format_log_event(
                    "admin_alert_sent",
                    alert_event=event,
                    admin_user_id=self.admin_line_user_id,
                    error_type=payload.get("error_type"),
                )
            )
        except Exception as err:
            self.logger.exception(
                format_log_event(
                    "admin_alert_failed",
                    alert_event=event,
                    admin_user_id=self.admin_line_user_id,
                    error_type=type(err).__name__,
                )
            )

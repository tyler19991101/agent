from __future__ import annotations

import json
import secrets
from typing import Any, Dict, Optional

from secretary_agent.config import Settings
from secretary_agent.memory import SQLiteStore


class BrowserAutomationManager:
    def __init__(self, *, settings: Settings, store: SQLiteStore):
        self.settings = settings
        self.store = store

    @property
    def enabled(self) -> bool:
        return self.settings.browser_automation_enabled

    def create_checkpoint(
        self,
        *,
        run_id: int,
        memory_key: str,
        browser_request: Dict[str, Any],
        profile: Dict[str, Any],
        accounts: list[Dict[str, Any]],
    ) -> Dict[str, Any]:
        domain = str(browser_request.get("domain", "")).strip() or "unknown"
        intent = str(browser_request.get("intent", "")).strip() or "browser_execution"
        automation_id = self.store.create_automation_run(
            run_id=run_id,
            memory_key=memory_key,
            domain=domain,
            intent=intent,
            status="awaiting_sensitive_confirmation",
            request_payload={
                "browser_request": browser_request,
                "profile_snapshot": profile,
                "accounts_snapshot": accounts,
            },
        )
        token = secrets.token_urlsafe(24)
        prompt_text = "請先確認要使用的資料與自動操作內容，確認後我才會往下一步。"
        self.store.create_sensitive_checkpoint(
            token=token,
            run_id=run_id,
            memory_key=memory_key,
            checkpoint_type="browser_review",
            prompt_text=prompt_text,
            payload={"automation_id": automation_id},
        )
        return {
            "automation_id": automation_id,
            "checkpoint_token": token,
            "prompt_text": prompt_text,
        }

    def build_review_page_context(self, checkpoint_row: Any) -> Dict[str, Any]:
        payload = json.loads(checkpoint_row["payload_json"] or "{}")
        automation_id = int(payload.get("automation_id", 0))
        automation = self.store.get_automation_run(automation_id)
        request_payload = json.loads(automation["request_json"] or "{}") if automation else {}
        return {
            "checkpoint": checkpoint_row,
            "automation": automation,
            "request_payload": request_payload,
            "enabled": self.enabled,
        }

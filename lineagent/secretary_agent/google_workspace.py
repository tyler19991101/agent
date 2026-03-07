from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from secretary_agent.config import Settings


class GoogleWorkspaceError(RuntimeError):
    pass


class GoogleWorkspaceClient:
    CALENDAR_SCOPES = (
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/tasks",
        "openid",
        "email",
        "profile",
    )

    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def is_configured(self) -> bool:
        return bool(
            self.settings.google_client_id
            and self.settings.google_client_secret
            and self.settings.google_redirect_uri
        )

    def new_state_token(self) -> str:
        return secrets.token_urlsafe(24)

    def build_auth_url(self, state_token: str) -> str:
        query = urlencode(
            {
                "client_id": self.settings.google_client_id,
                "redirect_uri": self.settings.google_redirect_uri,
                "response_type": "code",
                "scope": " ".join(self.CALENDAR_SCOPES),
                "access_type": "offline",
                "include_granted_scopes": "true",
                "prompt": "consent",
                "state": state_token,
            }
        )
        return f"https://accounts.google.com/o/oauth2/v2/auth?{query}"

    def exchange_code(self, code: str) -> Dict[str, Any]:
        payload = urlencode(
            {
                "code": code,
                "client_id": self.settings.google_client_id,
                "client_secret": self.settings.google_client_secret,
                "redirect_uri": self.settings.google_redirect_uri,
                "grant_type": "authorization_code",
            }
        ).encode("utf-8")
        req = Request(
            "https://oauth2.googleapis.com/token",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urlopen(req, timeout=30) as resp:
            token_payload = json.loads(resp.read().decode("utf-8"))
        if "access_token" not in token_payload:
            raise GoogleWorkspaceError("Google token exchange failed")
        return token_payload

    def fetch_userinfo(self, token_payload: Dict[str, Any]) -> Dict[str, Any]:
        req = Request(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {token_payload['access_token']}"},
            method="GET",
        )
        with urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def create_event(self, token_payload: Dict[str, Any], event: Dict[str, Any]) -> Dict[str, Any]:
        service = build("calendar", "v3", credentials=self._credentials(token_payload), cache_discovery=False)
        created = (
            service.events()
            .insert(calendarId=self.settings.google_calendar_id, body=event)
            .execute()
        )
        return {
            "id": created.get("id", ""),
            "summary": created.get("summary", ""),
            "html_link": created.get("htmlLink", ""),
            "status": created.get("status", ""),
        }

    def list_events(
        self,
        token_payload: Dict[str, Any],
        *,
        time_min: Optional[str] = None,
        time_max: Optional[str] = None,
        max_results: int = 10,
    ) -> Dict[str, Any]:
        service = build("calendar", "v3", credentials=self._credentials(token_payload), cache_discovery=False)
        items = (
            service.events()
            .list(
                calendarId=self.settings.google_calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
            .get("items", [])
        )
        return {"items": items}

    def create_task(self, token_payload: Dict[str, Any], task: Dict[str, Any]) -> Dict[str, Any]:
        service = build("tasks", "v1", credentials=self._credentials(token_payload), cache_discovery=False)
        created = (
            service.tasks()
            .insert(tasklist=self.settings.google_tasklist_id, body=task)
            .execute()
        )
        return {
            "id": created.get("id", ""),
            "title": created.get("title", ""),
            "status": created.get("status", ""),
            "self_link": created.get("selfLink", ""),
            "web_view_link": created.get("webViewLink", ""),
        }

    def list_tasks(self, token_payload: Dict[str, Any], *, show_completed: bool = False, max_results: int = 10) -> Dict[str, Any]:
        service = build("tasks", "v1", credentials=self._credentials(token_payload), cache_discovery=False)
        items = (
            service.tasks()
            .list(
                tasklist=self.settings.google_tasklist_id,
                showCompleted=show_completed,
                maxResults=max_results,
            )
            .execute()
            .get("items", [])
        )
        return {"items": items}

    def complete_task(self, token_payload: Dict[str, Any], *, task_id: str) -> Dict[str, Any]:
        service = build("tasks", "v1", credentials=self._credentials(token_payload), cache_discovery=False)
        updated = (
            service.tasks()
            .patch(
                tasklist=self.settings.google_tasklist_id,
                task=task_id,
                body={"status": "completed", "completed": datetime.now(timezone.utc).isoformat()},
            )
            .execute()
        )
        return {
            "id": updated.get("id", ""),
            "title": updated.get("title", ""),
            "status": updated.get("status", ""),
        }

    def _credentials(self, token_payload: Dict[str, Any]) -> Credentials:
        creds = Credentials(
            token=token_payload.get("access_token"),
            refresh_token=token_payload.get("refresh_token"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=self.settings.google_client_id,
            client_secret=self.settings.google_client_secret,
            scopes=self.CALENDAR_SCOPES,
        )
        expiry_value = token_payload.get("expiry")
        if expiry_value:
            try:
                creds.expiry = datetime.fromisoformat(expiry_value)
            except ValueError:
                pass
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleAuthRequest())
            token_payload["access_token"] = creds.token
            if creds.expiry:
                token_payload["expiry"] = creds.expiry.isoformat()
        return creds

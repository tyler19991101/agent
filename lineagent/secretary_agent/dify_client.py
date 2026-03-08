import json
import uuid
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from secretary_agent.models import PlannerResult
from secretary_agent.utils import extract_json_object, normalize_bool


class DifyAgentClient:
    _PROMPT_PATH = Path(__file__).resolve().parent.parent / "dify" / "SYSTEM_PROMPT.md"

    def __init__(self, *, api_key: str, base_url: str, user_prefix: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.user_prefix = user_prefix

    def plan(self, *, memory_key: str, user_goal: str, runtime_context: Dict[str, Any]) -> PlannerResult:
        prompt = self._build_prompt(user_goal=user_goal, runtime_context=runtime_context)
        answer = self._chat(query=prompt, user=f"{self.user_prefix}:{memory_key}")
        return self._parse_answer(answer)

    def audio_to_text(self, *, memory_key: str, audio_bytes: bytes, filename: str, mime_type: str) -> str:
        user = f"{self.user_prefix}:{memory_key}"
        payload, content_type = self._build_multipart_form_data(
            fields={"user": user},
            files={"file": (filename, audio_bytes, mime_type)},
        )
        req = Request(
            f"{self.base_url}/audio-to-text",
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": content_type,
                "Accept": "application/json",
                "User-Agent": "curl/8.7.1",
            },
            method="POST",
        )
        try:
            with urlopen(req, timeout=90) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                text = str(body.get("text", "")).strip()
                return text
        except HTTPError as err:
            detail = err.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Dify HTTP error {err.code}: {detail}") from err
        except URLError as err:
            raise RuntimeError(f"Dify connection error: {err}") from err

    def _build_prompt(self, *, user_goal: str, runtime_context: Dict[str, Any]) -> str:
        context_json = json.dumps(runtime_context, ensure_ascii=False, indent=2)
        system_prompt = self._PROMPT_PATH.read_text(encoding="utf-8").strip()
        return (
            f"{system_prompt}\n\n"
            f"使用者最新目標：{user_goal}\n"
            f"目前執行上下文：\n{context_json}"
        )

    def _chat(self, *, query: str, user: str) -> str:
        payload = json.dumps(
            {
                "inputs": {},
                "query": query,
                "response_mode": "blocking",
                "conversation_id": "",
                "user": user,
            }
        ).encode("utf-8")
        req = Request(
            f"{self.base_url}/chat-messages",
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "curl/8.7.1",
            },
            method="POST",
        )
        try:
            with urlopen(req, timeout=90) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return str(body.get("answer", "")).strip()
        except HTTPError as err:
            detail = err.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Dify HTTP error {err.code}: {detail}") from err
        except URLError as err:
            raise RuntimeError(f"Dify connection error: {err}") from err

    def _parse_answer(self, answer: str) -> PlannerResult:
        try:
            payload = extract_json_object(answer)
        except Exception:
            return PlannerResult(final_reply=answer or "目前無法產生秘書回覆。", raw_answer=answer)

        return PlannerResult(
            task_type=str(payload.get("task_type", "information_request")),
            goal_summary=str(payload.get("goal_summary", "")),
            subtasks=list(payload.get("subtasks", []) or []),
            needed_inputs=list(payload.get("needed_inputs", []) or []),
            requires_approval=normalize_bool(payload.get("requires_approval", False)),
            approval_type=str(payload.get("approval_type", "decision")),
            approval_prompt=str(payload.get("approval_prompt", "")),
            draft_user_reply=str(payload.get("draft_user_reply", "")),
            final_reply=str(payload.get("final_reply", "")),
            options=list(payload.get("options", []) or []),
            recommendation=str(payload.get("recommendation", "")),
            rationale=list(payload.get("rationale", []) or []),
            action_links=list(payload.get("action_links", []) or []),
            warnings=list(payload.get("warnings", []) or []),
            missing_info=list(payload.get("missing_info", []) or []),
            profile_updates=dict(payload.get("profile_updates", {}) or {}),
            account_updates=list(payload.get("account_updates", []) or []),
            memory_actions=list(payload.get("memory_actions", []) or []),
            calendar_action=dict(payload.get("calendar_action", {}) or {}),
            task_action=dict(payload.get("task_action", {}) or {}),
            browser_request=dict(payload.get("browser_request", {}) or {}),
            requested_outputs=list(payload.get("requested_outputs", []) or []),
            document_title=str(payload.get("document_title", "")),
            raw_answer=answer,
        )

    @staticmethod
    def _build_multipart_form_data(
        *,
        fields: Dict[str, str],
        files: Dict[str, tuple[str, bytes, str]],
    ) -> tuple[bytes, str]:
        boundary = f"----CodexBoundary{uuid.uuid4().hex}"
        body = bytearray()

        for name, value in fields.items():
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8")
            )
            body.extend(str(value).encode("utf-8"))
            body.extend(b"\r\n")

        for name, (filename, content, mime_type) in files.items():
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(
                (
                    f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                    f"Content-Type: {mime_type}\r\n\r\n"
                ).encode("utf-8")
            )
            body.extend(content)
            body.extend(b"\r\n")

        body.extend(f"--{boundary}--\r\n".encode("utf-8"))
        return bytes(body), f"multipart/form-data; boundary={boundary}"

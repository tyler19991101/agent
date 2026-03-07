import json
import uuid
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from secretary_agent.models import PlannerResult
from secretary_agent.utils import extract_json_object, normalize_bool


class DifyAgentClient:
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
        return (
            "你是一位真正的 AI 秘書代理，負責把使用者目標轉成可執行任務，必要時用 Dify 內建工具搜尋、閱讀網頁或影片字幕。\n"
            "請只輸出 JSON 物件，不要加任何說明文字。輸出欄位固定如下：\n"
            "{\n"
            '  "task_type": "information_request | research_and_compare | trip_planning | action_prep",\n'
            '  "goal_summary": "string",\n'
            '  "subtasks": ["string"],\n'
            '  "needed_inputs": ["string"],\n'
            '  "requires_approval": true,\n'
            '  "approval_type": "missing_info | decision",\n'
            '  "approval_prompt": "string",\n'
            '  "draft_user_reply": "string",\n'
            '  "final_reply": "string",\n'
            '  "options": [{"title":"string","summary":"string","price":"string","link":"string"}],\n'
            '  "recommendation": "string",\n'
            '  "rationale": ["string"],\n'
            '  "action_links": [{"label":"string","url":"string"}],\n'
            '  "warnings": ["string"],\n'
            '  "missing_info": ["string"],\n'
            '  "profile_updates": {"key":"value"},\n'
            '  "account_updates": [{"service_name":"string","login_identifier":"string","display_name":"string","oauth_provider":"string","session_available":false}],\n'
            '  "memory_actions": ["save_profile | update_profile | forget_profile | save_account | forget_account"],\n'
            '  "calendar_action": {"operation":"create_event | update_event | cancel_event | list_events","summary":"string","description":"string","start":"ISO-8601","end":"ISO-8601"},\n'
            '  "task_action": {"operation":"create_task | complete_task | list_tasks | delete_task","title":"string","notes":"string","due":"ISO-8601","task_id":"string"},\n'
            '  "browser_request": {"domain":"string","intent":"string","target_items":["string"],"user_profile_fields_needed":["string"],"stop_before_payment":true},\n'
            '  "requested_outputs": ["txt | docx | pdf"],\n'
            '  "document_title": "string"\n'
            "}\n"
            "規則：\n"
            "1. 一律使用繁體中文。\n"
            "2. 如果資訊不足，使用 needed_inputs/missing_info，並讓 approval_prompt 變成簡短追問。\n"
            "3. 如果已經可以做出建議，final_reply 要是手機可讀的秘書式報告。\n"
            "4. 如果需要使用者做選擇或確認，requires_approval=true。\n"
            "5. action_links 只能放官方或可信賴站點的下一步連結。\n"
            "6. profile_updates 只填可穩定記住的偏好。\n\n"
            "6-1. account_updates 可放需要長期記住的會員帳號識別資料，例如常用 email 或會員編號，但不要放密碼、信用卡、OTP。\n"
            "6-2. 若使用者要你記住、更新或忘記長期資料，請用 memory_actions、profile_updates、account_updates 表達。\n"
            "6-3. 若任務是建立 Google Calendar 行程或 Google Tasks 提醒，請用 calendar_action 或 task_action 輸出結構化操作需求。\n"
            "6-4. 若任務是要系統代為操作網站到付款前，請用 browser_request 描述，不可假裝已經完成付款。\n\n"
            "7. 如果使用者要求輸出成 Word、PDF、TXT 或檔案，請在 requested_outputs 明確列出格式。\n"
            "8. document_title 要給出適合檔案命名的人類可讀標題。\n\n"
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

import json
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from secretary_agent.models import PlannerResult
from secretary_agent.utils import extract_json_object, normalize_bool


class DifyAgentClient:
    _ALLOWED_CONVERSATION_MODES = {"new_task", "continue_task", "casual_reply"}
    _ALLOWED_CONTEXT_USAGE = {
        "none",
        "recent_task",
        "pending_approval",
        "quoted_message",
        "recent_image",
    }
    _ALLOWED_CALENDAR_OPERATIONS = {
        "create_event",
        "update_event",
        "cancel_event",
        "delete_event",
        "list_events",
    }
    _ALLOWED_TASK_OPERATIONS = {
        "create_task",
        "update_task",
        "complete_task",
        "list_tasks",
        "delete_task",
    }
    _ALLOWED_MEMORY_ACTIONS = {
        "save_profile",
        "update_profile",
        "forget_profile",
        "save_account",
        "forget_account",
    }
    _ALLOWED_OUTPUT_FORMATS = {"txt", "docx", "pdf"}

    def __init__(self, *, api_key: str, base_url: str, user_prefix: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.user_prefix = user_prefix

    def plan(
        self,
        *,
        memory_key: str,
        user_goal: str,
        runtime_context: Dict[str, Any],
        files: Optional[List[Dict[str, Any]]] = None,
    ) -> PlannerResult:
        prompt = self._build_prompt(user_goal=user_goal, runtime_context=runtime_context)
        answer = self._chat(query=prompt, user=f"{self.user_prefix}:{memory_key}", files=files or [])
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
            f"使用者最新目標：{user_goal}\n"
            f"目前執行上下文：\n{context_json}"
        )

    def upload_file(
        self,
        *,
        memory_key: str,
        file_bytes: bytes,
        filename: str,
        mime_type: str,
    ) -> Dict[str, Any]:
        user = f"{self.user_prefix}:{memory_key}"
        payload, content_type = self._build_multipart_form_data(
            fields={"user": user},
            files={"file": (filename, file_bytes, mime_type)},
        )
        req = Request(
            f"{self.base_url}/files/upload",
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
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as err:
            detail = err.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Dify HTTP error {err.code}: {detail}") from err
        except URLError as err:
            raise RuntimeError(f"Dify connection error: {err}") from err

    def _chat(self, *, query: str, user: str, files: List[Dict[str, Any]]) -> str:
        payload = json.dumps(self._build_chat_payload(query=query, user=user, files=files)).encode("utf-8")
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

    @staticmethod
    def _build_chat_payload(*, query: str, user: str, files: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "inputs": {},
            "query": query,
            "response_mode": "blocking",
            "conversation_id": "",
            "user": user,
            "files": files,
        }

    def _parse_answer(self, answer: str) -> PlannerResult:
        try:
            payload = extract_json_object(answer)
        except Exception:
            return PlannerResult(final_reply=answer or "目前無法產生秘書回覆。", raw_answer=answer)

        calendar_action = self._normalize_action_payload(
            payload.get("calendar_action", {}),
            allowed_operations=self._ALLOWED_CALENDAR_OPERATIONS,
        )
        task_action = self._normalize_action_payload(
            payload.get("task_action", {}),
            allowed_operations=self._ALLOWED_TASK_OPERATIONS,
        )
        browser_request = self._normalize_browser_request(payload.get("browser_request", {}))
        memory_actions = self._normalize_memory_actions(payload.get("memory_actions", []))
        requested_outputs = self._normalize_requested_outputs(payload.get("requested_outputs", []))
        profile_updates = self._normalize_profile_updates(
            payload.get("profile_updates", {}),
            memory_actions=memory_actions,
        )
        account_updates = self._normalize_account_updates(
            payload.get("account_updates", []),
            memory_actions=memory_actions,
        )

        return PlannerResult(
            conversation_mode=self._normalize_conversation_mode(payload.get("conversation_mode")),
            context_usage=self._normalize_context_usage(payload.get("context_usage")),
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
            profile_updates=profile_updates,
            account_updates=account_updates,
            memory_actions=memory_actions,
            calendar_action=calendar_action,
            task_action=task_action,
            browser_request=browser_request,
            requested_outputs=requested_outputs,
            document_title=str(payload.get("document_title", "")),
            raw_answer=answer,
        )

    def _normalize_conversation_mode(self, raw_mode: Any) -> str:
        mode = str(raw_mode or "").strip()
        if mode in self._ALLOWED_CONVERSATION_MODES:
            return mode
        return "new_task"

    def _normalize_context_usage(self, raw_usage: Any) -> str:
        usage = str(raw_usage or "").strip()
        if usage in self._ALLOWED_CONTEXT_USAGE:
            return usage
        return "none"

    @staticmethod
    def _normalize_action_payload(raw_action: Any, *, allowed_operations: Set[str]) -> Dict[str, Any]:
        if not isinstance(raw_action, dict):
            return {}
        operation = str(raw_action.get("operation", "")).strip()
        if operation not in allowed_operations:
            return {}
        normalized = dict(raw_action)
        normalized["operation"] = operation
        return normalized

    @staticmethod
    def _normalize_browser_request(raw_request: Any) -> Dict[str, Any]:
        if not isinstance(raw_request, dict):
            return {}
        domain = str(raw_request.get("domain", "")).strip()
        intent = str(raw_request.get("intent", "")).strip()
        if not domain or not intent:
            return {}
        normalized = dict(raw_request)
        normalized["domain"] = domain
        normalized["intent"] = intent
        return normalized

    def _normalize_memory_actions(self, raw_actions: Any) -> List[str]:
        if not isinstance(raw_actions, list):
            return []
        normalized: List[str] = []
        for item in raw_actions:
            action = str(item).strip()
            if action in self._ALLOWED_MEMORY_ACTIONS and action not in normalized:
                normalized.append(action)
        return normalized

    def _normalize_requested_outputs(self, raw_outputs: Any) -> List[str]:
        if not isinstance(raw_outputs, list):
            return []
        normalized: List[str] = []
        for item in raw_outputs:
            fmt = str(item).strip().lower()
            if fmt in self._ALLOWED_OUTPUT_FORMATS and fmt not in normalized:
                normalized.append(fmt)
        return normalized

    @staticmethod
    def _normalize_profile_updates(raw_profile: Any, *, memory_actions: List[str]) -> Dict[str, Any]:
        if not isinstance(raw_profile, dict):
            return {}
        if not any(action in memory_actions for action in {"save_profile", "update_profile", "forget_profile"}):
            return {}
        return dict(raw_profile)

    @staticmethod
    def _normalize_account_updates(raw_accounts: Any, *, memory_actions: List[str]) -> List[Dict[str, Any]]:
        if not isinstance(raw_accounts, list):
            return []
        if not any(action in memory_actions for action in {"save_account", "forget_account"}):
            return []
        normalized: List[Dict[str, Any]] = []
        for item in raw_accounts:
            if not isinstance(item, dict):
                continue
            service_name = str(item.get("service_name", "")).strip()
            login_identifier = str(item.get("login_identifier", "")).strip()
            if "forget_account" in memory_actions:
                if not service_name:
                    continue
            elif not service_name or not login_identifier:
                continue
            normalized_item = dict(item)
            if service_name:
                normalized_item["service_name"] = service_name
            if login_identifier:
                normalized_item["login_identifier"] = login_identifier
            normalized.append(normalized_item)
        return normalized

    @staticmethod
    def _build_multipart_form_data(
        *,
        fields: Dict[str, str],
        files: Dict[str, Tuple[str, bytes, str]],
    ) -> Tuple[bytes, str]:
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

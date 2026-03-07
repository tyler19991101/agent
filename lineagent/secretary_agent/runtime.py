import json
import logging
import time
from datetime import datetime, timedelta, timezone
from threading import Event, Thread
from typing import Any, Dict, List, Optional

from secretary_agent.browser_automation import BrowserAutomationManager
from secretary_agent.config import Settings
from secretary_agent.artifact_generator import ArtifactGenerator
from secretary_agent.dify_client import DifyAgentClient
from secretary_agent.google_workspace import GoogleWorkspaceClient, GoogleWorkspaceError
from secretary_agent.logging_utils import format_log_event
from secretary_agent.memory import SQLiteStore
from secretary_agent.models import InboundMessage, PlannerResult, TaskRun
from secretary_agent.utils import (
    APPROVAL_KEYWORDS,
    REJECTION_KEYWORDS,
    RESET_COMMANDS,
    infer_requested_outputs,
    looks_like_short_followup,
    normalize_service_name,
    parse_memory_command,
    prefers_file_only_response,
)


class SecretaryRuntime:
    USER_SAFE_ERROR_TEXT = "目前系統發生異常，已通報 IT 人員協助處理，請稍後再試。"

    def __init__(
        self,
        *,
        settings: Settings,
        store: SQLiteStore,
        messenger: Any,
        agent_client: Optional[DifyAgentClient] = None,
        google_client: Optional[GoogleWorkspaceClient] = None,
        browser_automation: Optional[BrowserAutomationManager] = None,
    ):
        self.settings = settings
        self.store = store
        self.messenger = messenger
        self.agent_client = agent_client or DifyAgentClient(
            api_key=settings.dify_api_key,
            base_url=settings.dify_base_url,
            user_prefix=settings.dify_user_prefix,
        )
        self.google_client = google_client or GoogleWorkspaceClient(settings)
        self.browser_automation = browser_automation or BrowserAutomationManager(
            settings=settings,
            store=store,
        )
        self.artifact_generator = ArtifactGenerator(
            output_dir=settings.artifact_output_dir,
            public_base_url=settings.public_base_url,
        )
        self.stop_event = Event()
        self.worker: Optional[Thread] = None
        self.logger = logging.getLogger("lineagent.runtime")
        self._active_run_id_for_service_artifacts = 0
        self.current_memory_key_for_resolution = ""

    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        self.worker = Thread(target=self._worker_loop, daemon=True)
        self.worker.start()

    def handle_inbound_message(self, inbound: InboundMessage) -> None:
        self.logger.info(
            format_log_event(
                "inbound_received",
                memory_key=inbound.memory_key,
                source_type=inbound.source_type,
                source_id=inbound.source_id,
                user_id=inbound.user_id,
                line_event_id=inbound.line_event_id,
                quoted_message_id=inbound.quoted_message_id,
                reply_enabled=inbound.reply_enabled,
            )
        )
        self.store.prune_short_context(inbound.memory_key, self.settings.short_context_ttl_days)
        text = inbound.text.strip()
        if not text:
            self.logger.info(
                format_log_event(
                    "inbound_empty_text",
                    memory_key=inbound.memory_key,
                    source_id=inbound.source_id,
                    user_id=inbound.user_id,
                )
            )
            self._respond_immediate(inbound, "請直接告訴我你要我協助的事情。")
            return

        if text.lower() in RESET_COMMANDS or text in RESET_COMMANDS:
            self.store.clear_memory(inbound.memory_key)
            self.logger.info(
                format_log_event(
                    "memory_reset",
                    memory_key=inbound.memory_key,
                    source_id=inbound.source_id,
                    user_id=inbound.user_id,
                )
            )
            self._send_immediate_without_history(inbound, "已清除這個 LINE 身分的對話與偏好記憶。")
            return

        memory_command = parse_memory_command(text)
        if memory_command:
            self._handle_memory_command(inbound, memory_command)
            return

        self.store.append_history(inbound.memory_key, "user", text)

        pending = self.store.get_open_approval(inbound.memory_key)
        quoted_text = self.store.get_bot_message_content(inbound.quoted_message_id)
        if pending and self._should_resume_pending(pending, inbound, quoted_text):
            self.store.resolve_approval(int(pending["id"]), text)
            self.store.add_artifact(
                int(pending["run_id"]),
                kind="approval_response",
                content=text,
                ref_key=f"approval:{pending['id']}",
                metadata={"quoted_message_id": inbound.quoted_message_id or ""},
            )
            self.store.update_run_status(
                int(pending["run_id"]),
                status="queued",
                current_phase="approval_resolved",
            )
            self.logger.info(
                format_log_event(
                    "approval_resolved",
                    run_id=pending["run_id"],
                    approval_id=pending["id"],
                    memory_key=inbound.memory_key,
                    source_id=inbound.source_id,
                    user_id=inbound.user_id,
                )
            )
            self._respond_immediate(inbound, "收到你的回覆，我繼續處理並整理結果。")
            return

        run_id, created = self.store.create_task_run(
            memory_key=inbound.memory_key,
            user_goal=text,
            normalized_goal=text,
            source_payload={
                "source_type": inbound.source_type,
                "source_id": inbound.source_id,
                "user_id": inbound.user_id,
                "quoted_message_id": inbound.quoted_message_id,
                "quoted_text": quoted_text,
                "received_at": inbound.received_at.isoformat(),
            },
            external_event_id=inbound.line_event_id,
        )
        self.store.add_artifact(
            run_id,
            kind="inbound_message",
            content=text,
            ref_key=inbound.line_event_id,
            metadata={"quoted_text": quoted_text},
        )
        self.logger.info(
            format_log_event(
                "task_queued",
                run_id=run_id,
                created=created,
                memory_key=inbound.memory_key,
                source_id=inbound.source_id,
                user_id=inbound.user_id,
                line_event_id=inbound.line_event_id,
            )
        )
        if created:
            ack = "任務已收到，我會先規劃並整理可執行方案，再把結果推送給你。"
        else:
            ack = "這則訊息我已收到過，正在處理中。"
        if inbound.reply_enabled:
            self._reply(inbound.reply_token, ack, inbound.memory_key)

    def process_next_run(self) -> bool:
        run = self.store.claim_next_run()
        if not run:
            return False
        self.logger.info(
            format_log_event(
                "task_claimed",
                run_id=run.id,
                memory_key=run.memory_key,
                status=run.status,
                current_phase=run.current_phase,
            )
        )
        self._process_run(run)
        return True

    def _worker_loop(self) -> None:
        while not self.stop_event.is_set():
            processed = self.process_next_run()
            if not processed:
                time.sleep(self.settings.worker_poll_seconds)

    def _process_run(self, run: TaskRun) -> None:
        try:
            self.logger.info(
                format_log_event(
                    "task_processing_started",
                    run_id=run.id,
                    memory_key=run.memory_key,
                    task_type=run.task_type,
                    current_phase=run.current_phase,
                )
            )
            self.store.prune_short_context(run.memory_key, self.settings.short_context_ttl_days)
            context = self.store.build_runtime_context(run.id, run.memory_key)
            current_local = datetime.now().astimezone()
            context["current_datetime_local"] = current_local.isoformat()
            context["current_date_local"] = current_local.date().isoformat()
            context["current_timezone"] = str(current_local.tzinfo or "UTC")
            planning_goal = self._build_planning_goal(run.user_goal, context)
            self.store.add_step(
                run.id,
                step_type="plan",
                actor="coordinator_agent",
                status="started",
                input_payload={"goal": planning_goal, "context": context},
            )
            plan = self.agent_client.plan(
                memory_key=run.memory_key,
                user_goal=planning_goal,
                runtime_context=context,
            )
            self.logger.info(
                format_log_event(
                    "task_plan_completed",
                    run_id=run.id,
                    memory_key=run.memory_key,
                    task_type=plan.task_type,
                    requires_approval=plan.requires_approval,
                    requested_outputs=",".join(plan.requested_outputs),
                )
            )
            self.store.add_step(
                run.id,
                step_type="plan",
                actor="coordinator_agent",
                status="completed",
                output_payload=self._planner_to_dict(plan),
            )
            self._apply_planner_memory_updates(run.memory_key, plan)

            plan.requested_outputs = self._merge_requested_outputs(run.user_goal, plan.requested_outputs)

            normalized_goal = plan.goal_summary or run.user_goal
            if plan.requires_approval or plan.needed_inputs or plan.missing_info:
                prompt = self._build_approval_prompt(plan)
                approval_id = self.store.create_pending_approval(
                    run_id=run.id,
                    memory_key=run.memory_key,
                    approval_type=plan.approval_type or "decision",
                    prompt_text=prompt,
                    options=plan.options,
                )
                self.store.update_run_status(
                    run.id,
                    status="waiting_approval",
                    task_type=plan.task_type,
                    requires_approval=True,
                    current_phase="waiting_approval",
                    normalized_goal=normalized_goal,
                )
                push_target = self._memory_key_to_push_target(run.memory_key)
                message_ids = self.messenger.push_text(push_target, prompt)
                self.logger.info(
                    format_log_event(
                        "task_waiting_approval",
                        run_id=run.id,
                        approval_id=approval_id,
                        memory_key=run.memory_key,
                        approval_type=plan.approval_type or "decision",
                    )
                )
                self.store.store_bot_messages(message_ids, self.messenger.split_for_storage(prompt))
                if message_ids:
                    self.store.set_approval_prompt_message(approval_id, message_ids[0])
                self.store.append_history(run.memory_key, "assistant", prompt)
                return

            google_result = self._handle_google_workspace_action(run, plan)
            if google_result.get("status") == "awaiting_google_auth":
                return
            if google_result.get("final_reply"):
                if google_result.get("authoritative"):
                    plan.final_reply = str(google_result["final_reply"])
                    plan.action_links = list(google_result.get("action_links", []) or [])
                else:
                    plan.final_reply = self._merge_text(plan.final_reply, str(google_result["final_reply"]))
                    if google_result.get("action_links"):
                        plan.action_links.extend(google_result["action_links"])

            browser_result = self._handle_browser_request(run, plan)
            if browser_result.get("status") == "awaiting_sensitive_confirmation":
                return
            if browser_result.get("final_reply"):
                plan.final_reply = self._merge_text(plan.final_reply, str(browser_result["final_reply"]))
            if browser_result.get("action_links"):
                plan.action_links.extend(browser_result["action_links"])

            final_text = self._build_final_text(plan)
            generated_artifacts = self._generate_requested_artifacts(run.id, run.user_goal, plan, final_text)
            if generated_artifacts:
                self.logger.info(
                    format_log_event(
                        "artifacts_generated",
                        run_id=run.id,
                        memory_key=run.memory_key,
                        artifact_count=len(generated_artifacts),
                        formats=",".join(item["format"] for item in generated_artifacts),
                    )
                )
            if generated_artifacts:
                if prefers_file_only_response(run.user_goal):
                    final_text = self._build_file_only_text(generated_artifacts)
                else:
                    final_text = self._append_artifact_links(final_text, generated_artifacts)
            self.store.add_artifact(run.id, kind="final_report", content=final_text)
            self.store.update_run_status(
                run.id,
                status="completed",
                task_type=plan.task_type,
                requires_approval=False,
                current_phase="completed",
                normalized_goal=normalized_goal,
                finished=True,
            )
            push_target = self._memory_key_to_push_target(run.memory_key)
            message_ids = self.messenger.push_text(push_target, final_text)
            self.logger.info(
                format_log_event(
                    "task_completed",
                    run_id=run.id,
                    memory_key=run.memory_key,
                    task_type=plan.task_type,
                    message_count=len(message_ids),
                    final_chars=len(final_text),
                )
            )
            self.store.store_bot_messages(message_ids, self.messenger.split_for_storage(final_text))
            self.store.append_history(run.memory_key, "assistant", final_text)
        except Exception as err:
            self.logger.exception(
                format_log_event(
                    "task_failed",
                    run_id=run.id,
                    memory_key=run.memory_key,
                    current_phase=run.current_phase,
                    error_type=type(err).__name__,
                )
            )
            self.store.add_step(
                run.id,
                step_type="plan",
                actor="runtime",
                status="failed",
                output_payload={"error": str(err)},
            )
            self.store.update_run_status(
                run.id,
                status="failed",
                current_phase="failed",
                error=str(err),
                finished=True,
            )
            push_target = self._memory_key_to_push_target(run.memory_key)
            self.messenger.push_text(push_target, self.USER_SAFE_ERROR_TEXT)
            self.store.append_history(run.memory_key, "assistant", self.USER_SAFE_ERROR_TEXT)

    def _build_planning_goal(self, user_goal: str, context: Dict[str, Any]) -> str:
        artifacts = context.get("artifacts", [])
        approval_responses = [
            str(item.get("content", "")).strip()
            for item in artifacts
            if item.get("kind") == "approval_response" and str(item.get("content", "")).strip()
        ]
        if not approval_responses:
            return user_goal

        lines = [
            f"原始任務：{user_goal}",
            "使用者針對前一次追問補充的資訊如下，請把這些內容視為同一個任務的更新條件，而不是新的獨立任務：",
        ]
        for idx, response in enumerate(approval_responses, start=1):
            lines.append(f"{idx}. {response}")
        lines.append("請整合原始任務與補充資訊後，再決定是否仍需追問。")
        return "\n".join(lines)

    def _build_final_text(self, plan: PlannerResult) -> str:
        base_text = plan.final_reply or plan.draft_user_reply or "任務已完成，但沒有取得可顯示的結果。"
        if not plan.action_links:
            return base_text

        existing_text = base_text.strip()
        existing_urls = {str(item.get("url", "")).strip() for item in plan.action_links if item.get("url")}
        if existing_text and any(url and url in existing_text for url in existing_urls):
            return base_text

        lines = [existing_text] if existing_text else []
        lines.append("相關連結：")
        for item in plan.action_links:
            label = str(item.get("label", "")).strip() or "連結"
            url = str(item.get("url", "")).strip()
            if not url:
                continue
            lines.append(f"- {label}: {url}")
        return "\n".join(lines)

    def _merge_requested_outputs(self, user_goal: str, requested_outputs: List[str]) -> List[str]:
        merged: List[str] = []
        for fmt in list(requested_outputs) + infer_requested_outputs(user_goal):
            if fmt not in {"txt", "docx", "pdf"}:
                continue
            if fmt not in merged:
                merged.append(fmt)
        return merged

    def _generate_requested_artifacts(
        self,
        run_id: int,
        user_goal: str,
        plan: PlannerResult,
        final_text: str,
    ) -> List[Dict[str, str]]:
        if not plan.requested_outputs:
            return []
        title = plan.document_title or plan.goal_summary or user_goal[:40] or "report"
        generated = self.artifact_generator.generate(
            title=title,
            content=final_text,
            output_formats=plan.requested_outputs,
        )
        artifacts: List[Dict[str, str]] = []
        for item in generated:
            self.store.add_artifact(
                run_id,
                kind="generated_file",
                ref_key=item.token,
                content=item.filename,
                metadata={
                    "format": item.format,
                    "path": item.path,
                    "filename": item.filename,
                    "url": item.url,
                },
            )
            artifacts.append(
                {
                    "format": item.format,
                    "filename": item.filename,
                    "url": item.url,
                    "path": item.path,
                }
            )
        return artifacts

    def _append_artifact_links(self, text: str, artifacts: List[Dict[str, str]]) -> str:
        lines = [text.strip()] if text.strip() else []
        lines.append("輸出檔案：")
        for item in artifacts:
            label = f"{item['format'].upper()} - {item['filename']}"
            target = item["url"] or item["path"]
            lines.append(f"- {label}: {target}")
        return "\n".join(lines)

    def _build_file_only_text(self, artifacts: List[Dict[str, str]]) -> str:
        lines = ["已完成，請下載檔案："]
        for item in artifacts:
            label = item["format"].upper()
            target = item["url"] or item["path"]
            lines.append(f"- {label}：{target}")
        return "\n".join(lines)

    def _should_resume_pending(self, pending: Any, inbound: InboundMessage, quoted_text: str) -> bool:
        prompt_message_id = pending["prompt_message_id"]
        if prompt_message_id and inbound.quoted_message_id == prompt_message_id:
            return True
        lower = inbound.text.strip().lower()
        if lower in APPROVAL_KEYWORDS or lower in REJECTION_KEYWORDS:
            return True
        return looks_like_short_followup(inbound.text) and bool(quoted_text or pending)

    def _build_approval_prompt(self, plan: PlannerResult) -> str:
        lines: List[str] = []
        if plan.draft_user_reply:
            lines.append(plan.draft_user_reply)
        if plan.approval_prompt:
            lines.append(plan.approval_prompt)
        if plan.options:
            lines.append("可選方案：")
            for idx, item in enumerate(plan.options, start=1):
                title = item.get("title", f"方案 {idx}")
                summary = item.get("summary", "")
                price = item.get("price", "")
                link = item.get("link", "")
                lines.append(f"{idx}. {title} {price}".strip())
                if summary:
                    lines.append(summary)
                if link:
                    lines.append(link)
        if not lines:
            lines.append("我需要你補充一些資訊，才能繼續處理這個任務。")
        return "\n".join(lines)

    def _reply(self, reply_token: str, text: str, memory_key: str) -> None:
        message_ids = self.messenger.reply_text(reply_token, text)
        self.logger.info(
            format_log_event(
                "reply_sent",
                memory_key=memory_key,
                message_count=len(message_ids),
                chars=len(text),
            )
        )
        self.store.store_bot_messages(message_ids, self.messenger.split_for_storage(text))
        self.store.append_history(memory_key, "assistant", text)

    def _send_immediate_without_history(self, inbound: InboundMessage, text: str) -> None:
        if inbound.reply_enabled and inbound.reply_token:
            message_ids = self.messenger.reply_text(inbound.reply_token, text)
        else:
            message_ids = self.messenger.push_text(self._memory_key_to_push_target(inbound.memory_key), text)
        self.logger.info(
            format_log_event(
                "immediate_message_sent",
                memory_key=inbound.memory_key,
                source_id=inbound.source_id,
                user_id=inbound.user_id,
                reply_enabled=inbound.reply_enabled,
                message_count=len(message_ids),
                chars=len(text),
            )
        )
        self.store.store_bot_messages(message_ids, self.messenger.split_for_storage(text))

    def _respond_immediate(self, inbound: InboundMessage, text: str) -> None:
        if inbound.reply_enabled and inbound.reply_token:
            self._reply(inbound.reply_token, text, inbound.memory_key)
            return
        message_ids = self.messenger.push_text(self._memory_key_to_push_target(inbound.memory_key), text)
        self.logger.info(
            format_log_event(
                "push_sent",
                memory_key=inbound.memory_key,
                source_id=inbound.source_id,
                user_id=inbound.user_id,
                message_count=len(message_ids),
                chars=len(text),
            )
        )
        self.store.store_bot_messages(message_ids, self.messenger.split_for_storage(text))
        self.store.append_history(inbound.memory_key, "assistant", text)

    def _memory_key_to_push_target(self, memory_key: str) -> str:
        return memory_key.split(":", 1)[1]

    def _handle_memory_command(self, inbound: InboundMessage, command: Dict[str, Any]) -> None:
        if command["type"] == "profile_save":
            profile = self.store.update_profile(inbound.memory_key, command.get("profile_updates", {}))
            self.store.log_memory_change(
                inbound.memory_key,
                command.get("change_type", "save_profile"),
                {"profile_updates": command.get("profile_updates", {}), "profile": profile},
            )
        elif command["type"] == "profile_forget":
            profile = self.store.remove_profile_fields(inbound.memory_key, command.get("fields", []))
            self.store.log_memory_change(
                inbound.memory_key,
                command.get("change_type", "forget_profile"),
                {"fields": command.get("fields", []), "profile": profile},
            )
        elif command["type"] == "account_save":
            for item in command.get("account_updates", []):
                self.store.upsert_connected_account(
                    inbound.memory_key,
                    service_name=normalize_service_name(str(item.get("service_name", ""))),
                    login_identifier=str(item.get("login_identifier", "")).strip(),
                    display_name=str(item.get("display_name", "")).strip(),
                    metadata={"source": "line_memory_command"},
                )
            self.store.log_memory_change(
                inbound.memory_key,
                command.get("change_type", "save_account"),
                {"account_updates": command.get("account_updates", [])},
            )
        elif command["type"] == "account_forget":
            deleted = self.store.forget_connected_account(
                inbound.memory_key,
                service_name=normalize_service_name(command.get("service_name", "")),
            )
            self.store.log_memory_change(
                inbound.memory_key,
                command.get("change_type", "forget_account"),
                {"service_name": command.get("service_name", ""), "deleted": deleted},
            )
        self.store.append_history(inbound.memory_key, "user", inbound.text)
        self._send_immediate_without_history(inbound, command["reply_text"])
        self.store.append_history(inbound.memory_key, "assistant", command["reply_text"])

    def _apply_planner_memory_updates(self, memory_key: str, plan: PlannerResult) -> None:
        if "forget_profile" in plan.memory_actions and plan.profile_updates:
            profile = self.store.remove_profile_fields(memory_key, list(plan.profile_updates.keys()))
            self.store.log_memory_change(
                memory_key,
                "planner_forget_profile",
                {"fields": list(plan.profile_updates.keys()), "profile": profile},
            )
            plan.profile_updates = {}
        if "forget_account" in plan.memory_actions:
            for item in plan.account_updates:
                service_name = normalize_service_name(str(item.get("service_name", "")).strip())
                login_identifier = str(item.get("login_identifier", "")).strip() or None
                if service_name:
                    self.store.forget_connected_account(
                        memory_key,
                        service_name=service_name,
                        login_identifier=login_identifier,
                    )
            if plan.account_updates:
                self.store.log_memory_change(
                    memory_key,
                    "planner_forget_account",
                    {"account_updates": plan.account_updates},
                )
            plan.account_updates = []
        if plan.profile_updates:
            profile = self.store.update_profile(memory_key, plan.profile_updates)
            self.store.log_memory_change(
                memory_key,
                "planner_profile_update",
                {"profile_updates": plan.profile_updates, "profile": profile},
            )
        for item in plan.account_updates:
            service_name = normalize_service_name(str(item.get("service_name", "")).strip())
            login_identifier = str(item.get("login_identifier", "")).strip()
            if not service_name or not login_identifier:
                continue
            self.store.upsert_connected_account(
                memory_key,
                service_name=service_name,
                login_identifier=login_identifier,
                display_name=str(item.get("display_name", "")).strip() or login_identifier,
                oauth_provider=str(item.get("oauth_provider", "")).strip(),
                session_available=bool(item.get("session_available", False)),
                metadata={k: v for k, v in item.items() if k not in {"service_name", "login_identifier", "display_name", "oauth_provider", "session_available"}},
            )
        if plan.account_updates:
            self.store.log_memory_change(
                memory_key,
                "planner_account_update",
                {"account_updates": plan.account_updates},
            )

    def _handle_google_workspace_action(self, run: TaskRun, plan: PlannerResult) -> Dict[str, Any]:
        action = plan.calendar_action or plan.task_action
        if not action:
            return {}
        try:
            self._active_run_id_for_service_artifacts = run.id
            self.current_memory_key_for_resolution = run.memory_key
            if not self.google_client.is_configured:
                return {
                    "final_reply": "目前尚未完成 Google 整合設定，因此暫時不能替你建立行程或提醒。",
                    "authoritative": True,
                }

            account = self.store.get_primary_connected_account(run.memory_key, "google")
            if not account:
                state_token = self.google_client.new_state_token()
                self.store.create_oauth_state(
                    state_token=state_token,
                    memory_key=run.memory_key,
                    provider="google",
                    run_id=run.id,
                    metadata={"goal": run.user_goal},
                )
                auth_url = f"{self.settings.public_base_url}/auth/google/start?state={state_token}"
                prompt = (
                    "要替你建立 Google 行程/提醒，請先完成 Google 授權：\n"
                    f"{auth_url}\n"
                    "授權完成後，我會自動繼續處理。"
                )
                self.store.update_run_status(
                    run.id,
                    status="waiting_approval",
                    current_phase="awaiting_google_auth",
                    requires_approval=True,
                )
                push_target = self._memory_key_to_push_target(run.memory_key)
                message_ids = self.messenger.push_text(push_target, prompt)
                self.store.store_bot_messages(message_ids, self.messenger.split_for_storage(prompt))
                self.store.append_history(run.memory_key, "assistant", prompt)
                return {"status": "awaiting_google_auth"}

            token_payload = self._account_token_payload(account)
            if plan.calendar_action:
                result = self._execute_calendar_action(token_payload, plan.calendar_action)
                self.store.upsert_connected_account(
                    run.memory_key,
                    service_name="google",
                    login_identifier=str(account["login_identifier"]),
                    display_name=str(account["display_name"]),
                    oauth_provider="google",
                    session_available=True,
                    metadata=token_payload,
                )
                return {
                    "final_reply": result["message"],
                    "action_links": result.get("action_links", []),
                    "authoritative": True,
                }
            if plan.task_action:
                result = self._execute_task_action(token_payload, plan.task_action)
                self.store.upsert_connected_account(
                    run.memory_key,
                    service_name="google",
                    login_identifier=str(account["login_identifier"]),
                    display_name=str(account["display_name"]),
                    oauth_provider="google",
                    session_available=True,
                    metadata=token_payload,
                )
                return {
                    "final_reply": result["message"],
                    "action_links": result.get("action_links", []),
                    "authoritative": True,
                }
        except GoogleWorkspaceError as err:
            self.logger.exception(
                format_log_event(
                    "google_workspace_action_failed",
                    run_id=run.id,
                    memory_key=run.memory_key,
                    error_type=type(err).__name__,
                )
            )
            return {
                "final_reply": "Google 行程/提醒處理失敗，已記錄錯誤並請你稍後再試。",
                "authoritative": True,
            }
        finally:
            self._active_run_id_for_service_artifacts = 0
            self.current_memory_key_for_resolution = ""
        return {}

    def _execute_calendar_action(self, token_payload: Dict[str, Any], action: Dict[str, Any]) -> Dict[str, Any]:
        operation = str(action.get("operation", "")).strip()
        event_id = str(action.get("event_id", "")).strip() or self._resolve_recent_service_id("google_event", action)
        if operation == "create_event":
            event = {
                "summary": str(action.get("summary", "")).strip() or "LINE 助理建立的行程",
                "description": str(action.get("description", "")).strip(),
                "start": {"dateTime": str(action.get("start", "")).strip()},
                "end": {"dateTime": str(action.get("end", "")).strip()},
            }
            timezone_name = str(action.get("timezone", "Asia/Taipei")).strip() or "Asia/Taipei"
            event["start"]["timeZone"] = timezone_name
            event["end"]["timeZone"] = timezone_name
            created = self.google_client.create_event(token_payload, event)
            self._store_service_artifact(
                run_kind="google_event",
                ref_key=created["id"],
                title=created["summary"],
                metadata={
                    "event_id": created["id"],
                    "summary": created["summary"],
                    "html_link": created.get("html_link", ""),
                    "status": created.get("status", ""),
                    "start": event["start"]["dateTime"],
                    "end": event["end"]["dateTime"],
                },
            )
            return {
                "message": f"已替你建立 Google 行程：{created['summary']}",
                "action_links": ([{"label": "開啟 Google Calendar", "url": created["html_link"]}] if created.get("html_link") else []),
            }
        if operation == "update_event":
            if not event_id:
                raise GoogleWorkspaceError("Missing event_id for update_event")
            patch_body: Dict[str, Any] = {}
            if str(action.get("summary", "")).strip():
                patch_body["summary"] = str(action.get("summary", "")).strip()
            if "description" in action:
                patch_body["description"] = str(action.get("description", "")).strip()
            if str(action.get("start", "")).strip():
                patch_body["start"] = {
                    "dateTime": str(action.get("start", "")).strip(),
                    "timeZone": str(action.get("timezone", "Asia/Taipei")).strip() or "Asia/Taipei",
                }
            if str(action.get("end", "")).strip():
                patch_body["end"] = {
                    "dateTime": str(action.get("end", "")).strip(),
                    "timeZone": str(action.get("timezone", "Asia/Taipei")).strip() or "Asia/Taipei",
                }
            updated = self.google_client.update_event(token_payload, event_id=event_id, event=patch_body)
            self._store_service_artifact(
                run_kind="google_event",
                ref_key=updated["id"],
                title=updated["summary"],
                metadata={
                    "event_id": updated["id"],
                    "summary": updated["summary"],
                    "html_link": updated.get("html_link", ""),
                    "status": updated.get("status", ""),
                    "start": patch_body.get("start", {}).get("dateTime", ""),
                    "end": patch_body.get("end", {}).get("dateTime", ""),
                },
            )
            return {
                "message": f"已替你調整 Google 行程：{updated['summary']}",
                "action_links": ([{"label": "開啟 Google Calendar", "url": updated["html_link"]}] if updated.get("html_link") else []),
            }
        if operation in {"cancel_event", "delete_event"}:
            if not event_id:
                raise GoogleWorkspaceError("Missing event_id for cancel_event")
            deleted = self.google_client.delete_event(token_payload, event_id=event_id)
            self._store_service_artifact(
                run_kind="google_event",
                ref_key=deleted["id"],
                title="cancelled",
                metadata={"event_id": deleted["id"], "status": deleted["status"]},
            )
            return {"message": "已替你取消 Google 行程。"}
        if operation == "list_events":
            now = datetime.now(timezone.utc)
            time_min = str(action.get("time_min", "")).strip() or now.isoformat()
            time_max = str(action.get("time_max", "")).strip() or (now + timedelta(days=7)).isoformat()
            listed = self.google_client.list_events(token_payload, time_min=time_min, time_max=time_max)
            items = listed.get("items", [])[:5]
            if not items:
                return {"message": "你的 Google Calendar 目前查不到符合條件的行程。"}
            lines = ["你近期的 Google 行程："]
            for item in items:
                start = item.get("start", {}).get("dateTime") or item.get("start", {}).get("date") or ""
                lines.append(f"- {item.get('summary', '未命名行程')}：{start}")
            return {"message": "\n".join(lines)}
        raise GoogleWorkspaceError(f"Unsupported calendar operation: {operation}")

    def _execute_task_action(self, token_payload: Dict[str, Any], action: Dict[str, Any]) -> Dict[str, Any]:
        operation = str(action.get("operation", "")).strip()
        task_id = str(action.get("task_id", "")).strip() or self._resolve_recent_service_id("google_task", action)
        if operation == "create_task":
            task = {
                "title": str(action.get("title", "")).strip() or "LINE 助理建立的提醒",
                "notes": str(action.get("notes", "")).strip(),
            }
            due = str(action.get("due", "")).strip()
            if due:
                task["due"] = due
            created = self.google_client.create_task(token_payload, task)
            self._store_service_artifact(
                run_kind="google_task",
                ref_key=created["id"],
                title=created["title"],
                metadata={
                    "task_id": created["id"],
                    "title": created["title"],
                    "status": created.get("status", ""),
                    "web_view_link": created.get("web_view_link", ""),
                    "due": due,
                },
            )
            return {
                "message": f"已替你建立 Google Tasks 提醒：{created['title']}",
                "action_links": ([{"label": "查看 Google Tasks", "url": created.get("web_view_link", "")}] if created.get("web_view_link") else []),
            }
        if operation == "update_task":
            if not task_id:
                raise GoogleWorkspaceError("Missing task_id for update_task")
            patch_body: Dict[str, Any] = {}
            if str(action.get("title", "")).strip():
                patch_body["title"] = str(action.get("title", "")).strip()
            if "notes" in action:
                patch_body["notes"] = str(action.get("notes", "")).strip()
            if "due" in action and str(action.get("due", "")).strip():
                patch_body["due"] = str(action.get("due", "")).strip()
            updated = self.google_client.update_task(token_payload, task_id=task_id, task=patch_body)
            self._store_service_artifact(
                run_kind="google_task",
                ref_key=updated["id"],
                title=updated["title"],
                metadata={
                    "task_id": updated["id"],
                    "title": updated["title"],
                    "status": updated.get("status", ""),
                    "web_view_link": updated.get("web_view_link", ""),
                    "due": patch_body.get("due", updated.get("due", "")),
                },
            )
            return {
                "message": f"已替你調整 Google Tasks 提醒：{updated['title']}",
                "action_links": ([{"label": "查看 Google Tasks", "url": updated.get("web_view_link", "")}] if updated.get("web_view_link") else []),
            }
        if operation == "list_tasks":
            listed = self.google_client.list_tasks(token_payload)
            items = listed.get("items", [])[:5]
            if not items:
                return {"message": "你的 Google Tasks 目前沒有待辦事項。"}
            lines = ["你目前的待辦事項："]
            for item in items:
                lines.append(f"- {item.get('title', '未命名待辦')}")
            return {"message": "\n".join(lines)}
        if operation == "complete_task":
            if not task_id:
                raise GoogleWorkspaceError("Missing task_id for complete_task")
            updated = self.google_client.complete_task(token_payload, task_id=task_id)
            self._store_service_artifact(
                run_kind="google_task",
                ref_key=updated["id"],
                title=updated["title"],
                metadata={"task_id": updated["id"], "title": updated["title"], "status": updated["status"]},
            )
            return {"message": f"已替你完成待辦：{updated['title']}"}
        if operation == "delete_task":
            if not task_id:
                raise GoogleWorkspaceError("Missing task_id for delete_task")
            deleted = self.google_client.delete_task(token_payload, task_id=task_id)
            self._store_service_artifact(
                run_kind="google_task",
                ref_key=deleted["id"],
                title="deleted",
                metadata={"task_id": deleted["id"], "status": deleted["status"]},
            )
            return {"message": "已替你刪除 Google Tasks 提醒。"}
        raise GoogleWorkspaceError(f"Unsupported task operation: {operation}")

    def _resolve_recent_service_id(self, kind: str, action: Dict[str, Any]) -> str:
        summary = str(action.get("summary", "")).strip()
        title = str(action.get("title", "")).strip()
        target_name = summary or title
        recent = self.store.get_recent_memory_artifacts(self.current_memory_key_for_resolution, kinds=[kind], limit=5)
        for row in recent:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except json.JSONDecodeError:
                metadata = {}
            candidate_name = str(metadata.get("summary") or metadata.get("title") or row["content"] or "").strip()
            if target_name and candidate_name and candidate_name != target_name:
                continue
            return str(metadata.get("event_id") or metadata.get("task_id") or row["ref_key"] or "")
        return ""

    def _store_service_artifact(self, *, run_kind: str, ref_key: str, title: str, metadata: Dict[str, Any]) -> None:
        run_id = getattr(self, "_active_run_id_for_service_artifacts", 0)
        if not run_id:
            return
        self.store.add_artifact(
            run_id,
            kind=run_kind,
            ref_key=ref_key,
            content=title or ref_key,
            metadata=metadata,
        )

    def _handle_browser_request(self, run: TaskRun, plan: PlannerResult) -> Dict[str, Any]:
        if not plan.browser_request:
            return {}
        self.logger.info(
            format_log_event(
                "browser_request_deferred",
                run_id=run.id,
                memory_key=run.memory_key,
                domain=plan.browser_request.get("domain", ""),
                intent=plan.browser_request.get("intent", ""),
            )
        )
        return {
            "final_reply": "網站自動操作到付款前的功能會放到下一階段，目前先提供個人記憶、行事曆與提醒事項服務。",
        }

    def _serialize_accounts(self, memory_key: str) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for row in self.store.get_connected_accounts(memory_key):
            results.append(
                {
                    "service_name": row["service_name"],
                    "login_identifier": row["login_identifier"],
                    "display_name": row["display_name"],
                    "oauth_provider": row["oauth_provider"],
                    "session_available": bool(row["session_available"]),
                }
            )
        return results

    @staticmethod
    def _account_token_payload(account_row: Any) -> Dict[str, Any]:
        import json

        try:
            return json.loads(account_row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _merge_text(primary: str, extra: str) -> str:
        primary = (primary or "").strip()
        extra = (extra or "").strip()
        if not primary:
            return extra
        if not extra:
            return primary
        if extra in primary:
            return primary
        return f"{primary}\n\n{extra}"

    @staticmethod
    def _planner_to_dict(plan: PlannerResult) -> Dict[str, Any]:
        return {
            "task_type": plan.task_type,
            "goal_summary": plan.goal_summary,
            "subtasks": plan.subtasks,
            "needed_inputs": plan.needed_inputs,
            "requires_approval": plan.requires_approval,
            "approval_type": plan.approval_type,
            "approval_prompt": plan.approval_prompt,
            "draft_user_reply": plan.draft_user_reply,
            "final_reply": plan.final_reply,
            "options": plan.options,
            "recommendation": plan.recommendation,
            "rationale": plan.rationale,
            "action_links": plan.action_links,
            "warnings": plan.warnings,
            "missing_info": plan.missing_info,
            "profile_updates": plan.profile_updates,
            "account_updates": plan.account_updates,
            "memory_actions": plan.memory_actions,
            "calendar_action": plan.calendar_action,
            "task_action": plan.task_action,
            "browser_request": plan.browser_request,
            "requested_outputs": plan.requested_outputs,
            "document_title": plan.document_title,
        }

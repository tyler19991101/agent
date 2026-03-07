import logging
import time
from threading import Event, Thread
from typing import Any, Dict, List, Optional

from secretary_agent.config import Settings
from secretary_agent.artifact_generator import ArtifactGenerator
from secretary_agent.dify_client import DifyAgentClient
from secretary_agent.logging_utils import format_log_event
from secretary_agent.memory import SQLiteStore
from secretary_agent.models import InboundMessage, PlannerResult, TaskRun
from secretary_agent.utils import (
    APPROVAL_KEYWORDS,
    REJECTION_KEYWORDS,
    RESET_COMMANDS,
    infer_requested_outputs,
    looks_like_short_followup,
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
    ):
        self.settings = settings
        self.store = store
        self.messenger = messenger
        self.agent_client = agent_client or DifyAgentClient(
            api_key=settings.dify_api_key,
            base_url=settings.dify_base_url,
            user_prefix=settings.dify_user_prefix,
        )
        self.artifact_generator = ArtifactGenerator(
            output_dir=settings.artifact_output_dir,
            public_base_url=settings.public_base_url,
        )
        self.stop_event = Event()
        self.worker: Optional[Thread] = None
        self.logger = logging.getLogger("lineagent.runtime")

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
            if plan.profile_updates:
                self.store.update_profile(run.memory_key, plan.profile_updates)

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
            "requested_outputs": plan.requested_outputs,
            "document_title": plan.document_title,
        }

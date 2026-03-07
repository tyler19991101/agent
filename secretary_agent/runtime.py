import time
from threading import Event, Thread
from typing import Any, Dict, List, Optional

from secretary_agent.config import Settings
from secretary_agent.dify_client import DifyAgentClient
from secretary_agent.memory import SQLiteStore
from secretary_agent.models import InboundMessage, PlannerResult, TaskRun
from secretary_agent.utils import (
    APPROVAL_KEYWORDS,
    REJECTION_KEYWORDS,
    RESET_COMMANDS,
    looks_like_short_followup,
)


class SecretaryRuntime:
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
        self.stop_event = Event()
        self.worker: Optional[Thread] = None

    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        self.worker = Thread(target=self._worker_loop, daemon=True)
        self.worker.start()

    def handle_inbound_message(self, inbound: InboundMessage) -> None:
        text = inbound.text.strip()
        if not text:
            self._reply(inbound.reply_token, "請直接告訴我你要我協助的事情。", inbound.memory_key)
            return

        if text.lower() in RESET_COMMANDS or text in RESET_COMMANDS:
            self.store.clear_memory(inbound.memory_key)
            message_ids = self.messenger.reply_text(
                inbound.reply_token,
                "已清除這個 LINE 身分的對話與偏好記憶。",
            )
            self.store.store_bot_messages(
                message_ids,
                self.messenger.split_for_storage("已清除這個 LINE 身分的對話與偏好記憶。"),
            )
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
            self._reply(inbound.reply_token, "收到你的回覆，我繼續處理並整理結果。", inbound.memory_key)
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
        if created:
            ack = "任務已收到，我會先規劃並整理可執行方案，再把結果推送給你。"
        else:
            ack = "這則訊息我已收到過，正在處理中。"
        self._reply(inbound.reply_token, ack, inbound.memory_key)

    def process_next_run(self) -> bool:
        run = self.store.claim_next_run()
        if not run:
            return False
        self._process_run(run)
        return True

    def _worker_loop(self) -> None:
        while not self.stop_event.is_set():
            processed = self.process_next_run()
            if not processed:
                time.sleep(self.settings.worker_poll_seconds)

    def _process_run(self, run: TaskRun) -> None:
        try:
            context = self.store.build_runtime_context(run.id, run.memory_key)
            self.store.add_step(
                run.id,
                step_type="plan",
                actor="coordinator_agent",
                status="started",
                input_payload={"goal": run.user_goal, "context": context},
            )
            plan = self.agent_client.plan(
                memory_key=run.memory_key,
                user_goal=run.user_goal,
                runtime_context=context,
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
                self.store.store_bot_messages(message_ids, self.messenger.split_for_storage(prompt))
                if message_ids:
                    self.store.set_approval_prompt_message(approval_id, message_ids[0])
                self.store.append_history(run.memory_key, "assistant", prompt)
                return

            final_text = plan.final_reply or plan.draft_user_reply or "任務已完成，但沒有取得可顯示的結果。"
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
            self.store.store_bot_messages(message_ids, self.messenger.split_for_storage(final_text))
            self.store.append_history(run.memory_key, "assistant", final_text)
        except Exception as err:
            error_text = f"處理任務時發生錯誤：{err}"
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
            self.messenger.push_text(push_target, error_text)
            self.store.append_history(run.memory_key, "assistant", error_text)

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
        self.store.store_bot_messages(message_ids, self.messenger.split_for_storage(text))
        self.store.append_history(memory_key, "assistant", text)

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
        }

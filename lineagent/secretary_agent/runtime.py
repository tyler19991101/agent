import json
import logging
import os
import time
from datetime import datetime, timezone
from threading import Event, Thread
from typing import Any, Dict, List, Optional, Tuple

from secretary_agent.browser_automation import BrowserAutomationManager
from secretary_agent.admin_notifier import AdminNotifier
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
        admin_notifier: Optional[AdminNotifier] = None,
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
        self.admin_notifier = admin_notifier
        self._active_run_id_for_service_artifacts = 0
        self.current_memory_key_for_resolution = ""
        self._last_image_cleanup_at = 0.0

    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        self._cleanup_expired_image_assets()
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
        if pending:
            pending_decision = self._decide_pending_followup(pending, inbound, quoted_text)
            if pending_decision == "casual_reply":
                return
            if pending_decision == "new_task":
                pending = None
        if pending and self._should_resume_pending(pending, inbound, quoted_text):
            pending_run = self.store.get_task_run(int(pending["run_id"]))
            if pending_run.current_phase == "awaiting_google_auth" and self._looks_like_google_auth_decline(text):
                self.store.resolve_approval(int(pending["id"]), text)
                self.store.update_run_status(
                    pending_run.id,
                    status="failed",
                    current_phase="google_auth_declined",
                    error="User declined Google auth",
                    finished=True,
                )
                rewritten_goal = (
                    f"{pending_run.user_goal}\n"
                    "補充要求：不要建立 Google Calendar 或 Google Tasks，也不要要求 Google 授權，只提供純文字規劃結果。"
                )
                new_run_id, created = self.store.create_task_run(
                    memory_key=inbound.memory_key,
                    user_goal=rewritten_goal,
                    normalized_goal=rewritten_goal,
                    source_payload={
                        "source_type": inbound.source_type,
                        "source_id": inbound.source_id,
                        "user_id": inbound.user_id,
                        "quoted_message_id": inbound.quoted_message_id,
                        "quoted_text": quoted_text,
                        "received_at": inbound.received_at.isoformat(),
                        "supersedes_run_id": pending_run.id,
                    },
                    external_event_id=inbound.line_event_id,
                )
                self.logger.info(
                    format_log_event(
                        "google_auth_declined_replanned",
                        old_run_id=pending_run.id,
                        new_run_id=new_run_id,
                        created=created,
                        memory_key=inbound.memory_key,
                        source_id=inbound.source_id,
                        user_id=inbound.user_id,
                    )
                )
                self._respond_immediate(inbound, "收到，我不會建立 Google 行事曆或提醒，改成只提供文字規劃。")
                return
            approval_metadata = {"quoted_message_id": inbound.quoted_message_id or ""}
            selected_option = self._match_pending_approval_option(pending, text)
            if self._approval_requires_option_selection(pending) and not selected_option:
                self._respond_immediate(
                    inbound,
                    "我還沒辨識到你要選哪一個事項，請直接回覆選項編號或完整事項名稱，例如 1。",
                )
                return
            if selected_option:
                approval_metadata["selected_option"] = selected_option
            self.store.resolve_approval(int(pending["id"]), text)
            self.store.add_artifact(
                int(pending["run_id"]),
                kind="approval_response",
                content=text,
                ref_key=f"approval:{pending['id']}",
                metadata=approval_metadata,
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
        self.store.attach_image_assets_to_run(run_id, inbound.image_asset_ids)
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
            self._maybe_cleanup_expired_image_assets()
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
            self._maybe_attach_recent_image_context(run, context)
            current_local = datetime.now().astimezone()
            context["current_datetime_local"] = current_local.isoformat()
            context["current_date_local"] = current_local.date().isoformat()
            context["current_timezone"] = str(current_local.tzinfo or "UTC")
            planning_goal = self._build_planning_goal(run.user_goal, context)
            dify_files = self._prepare_dify_image_files(run, context)
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
                files=dify_files,
            )
            plan = self._replan_if_google_query_contract_missing(
                run=run,
                planning_goal=planning_goal,
                runtime_context=context,
                plan=plan,
                files=dify_files,
            )
            plan = self._repair_image_missing_info_plan(
                run=run,
                planning_goal=planning_goal,
                runtime_context=context,
                plan=plan,
                files=dify_files,
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
            if google_result.get("status") in {"awaiting_google_auth", "awaiting_approval"}:
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
            self._update_image_analysis_summary(run.id, plan, final_text)
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
            if self.admin_notifier:
                self.admin_notifier.notify_system_error(
                    event="task_failed",
                    summary="使用者任務處理失敗",
                    fields={
                        "run_id": run.id,
                        "memory_key": run.memory_key,
                        "error_type": type(err).__name__,
                    },
                )

    def _build_planning_goal(self, user_goal: str, context: Dict[str, Any]) -> str:
        if context.get("image_assets") and not user_goal.strip():
            return "請先做通用看圖分析，說明圖片內容、可提取的重點，以及是否需要我再補充用途。"
        recent_image_summary = context.get("recent_image_summary")
        if recent_image_summary and user_goal.strip():
            summary_text = str(recent_image_summary.get("summary", "")).strip()
            visible_objects = recent_image_summary.get("visible_objects") or []
            visible_text = recent_image_summary.get("visible_text") or []
            scene_or_context = str(recent_image_summary.get("scene_or_context", "")).strip()
            suggested_followups = recent_image_summary.get("suggested_followups") or []
            lines = [
                f"使用者最新目標：{user_goal}",
                "補充上下文：這則訊息很可能是在追問剛剛上傳的圖片。",
                "請優先根據已存在的圖片基礎分析摘要回答；只有當摘要不足以回答時，才再結合原圖做更細的分析。",
            ]
            if summary_text:
                lines.append(f"圖片摘要：{summary_text}")
            if visible_objects:
                lines.append("可見物件：" + "、".join(str(item) for item in visible_objects[:10]))
            if visible_text:
                lines.append("可辨識文字：" + "、".join(str(item) for item in visible_text[:10]))
            if scene_or_context:
                lines.append(f"場景/情境：{scene_or_context}")
            if suggested_followups:
                lines.append("可延伸追問：" + "、".join(str(item) for item in suggested_followups[:6]))
            return "\n".join(lines)
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

    def _replan_if_google_query_contract_missing(
        self,
        *,
        run: TaskRun,
        planning_goal: str,
        runtime_context: Dict[str, Any],
        plan: PlannerResult,
        files: List[Dict[str, Any]],
    ) -> PlannerResult:
        if not self._needs_google_query_contract_retry(run.user_goal, plan):
            return plan

        repair_goal = (
            f"{planning_goal}\n\n"
            "補充要求：這是一個 Google 個人助理查詢。"
            "如果使用者是在查行程，必須輸出 calendar_action.operation=list_events 與 time_min/time_max；"
            "如果使用者是在查提醒或待辦，必須輸出 task_action.operation=list_tasks。"
            "不要只回一般摘要。"
        )
        repaired_plan = self.agent_client.plan(
            memory_key=run.memory_key,
            user_goal=repair_goal,
            runtime_context=runtime_context,
            files=files,
        )
        self.logger.info(
            format_log_event(
                "task_replanned_for_google_query_contract",
                run_id=run.id,
                memory_key=run.memory_key,
                original_task_type=plan.task_type,
                repaired_task_type=repaired_plan.task_type,
            )
        )
        return repaired_plan

    def _repair_image_missing_info_plan(
        self,
        *,
        run: TaskRun,
        planning_goal: str,
        runtime_context: Dict[str, Any],
        plan: PlannerResult,
        files: List[Dict[str, Any]],
    ) -> PlannerResult:
        image_assets = runtime_context.get("image_assets") or []
        user_goal = run.user_goal.strip()
        if not image_assets or user_goal:
            return plan
        if not (plan.requires_approval or plan.needed_inputs or plan.missing_info):
            return plan

        repair_goal = (
            f"{planning_goal}\n\n"
            "補充要求：這是一個純圖片上傳任務，圖片有效且可讀。"
            "不要把『沒有文字問題』判成缺資料。"
            "你必須先直接提供第一輪通用看圖分析，內容至少包含可見主體、場景、可辨識文字與可能用途。"
            "只有在圖片本身無法辨識、毀損、空白，或真的無法從圖中提取任何內容時，才可以 requires_approval=true。"
        )
        repaired_plan = self.agent_client.plan(
            memory_key=run.memory_key,
            user_goal=repair_goal,
            runtime_context=runtime_context,
            files=files,
        )
        if not (repaired_plan.requires_approval or repaired_plan.needed_inputs or repaired_plan.missing_info):
            self.logger.info(
                format_log_event(
                    "task_replanned_for_image_contract",
                    run_id=run.id,
                    memory_key=run.memory_key,
                    original_requires_approval=plan.requires_approval,
                    repaired_requires_approval=repaired_plan.requires_approval,
                )
            )
            return repaired_plan

        fallback_plan = PlannerResult(
            conversation_mode="new_task",
            context_usage="none",
            task_type="information_request",
            goal_summary="通用看圖分析",
            final_reply=(
                "我已收到這張圖片，會先用通用看圖方式幫你整理可見內容、場景與可能重點。"
                "如果你想知道更具體的細節，例如文字、品牌、位置或用途，也可以直接告訴我。"
            ),
            warnings=["模型未依圖片首輪分析合約回覆，已改用系統通用看圖兜底。"],
        )
        self.logger.info(
            format_log_event(
                "task_fallback_for_image_contract",
                run_id=run.id,
                memory_key=run.memory_key,
                original_requires_approval=plan.requires_approval,
                repaired_requires_approval=repaired_plan.requires_approval,
            )
        )
        return fallback_plan

    def _prepare_dify_image_files(self, run: TaskRun, runtime_context: Dict[str, Any]) -> List[Dict[str, Any]]:
        image_assets = runtime_context.get("image_assets", []) or []
        prepared_files: List[Dict[str, Any]] = []
        for asset in image_assets:
            path = str(asset.get("path", "")).strip()
            if not path or not os.path.isfile(path):
                self.store.mark_image_asset_missing(int(asset.get("id", 0)))
                continue
            with open(path, "rb") as file_obj:
                file_bytes = file_obj.read()
            upload = self.agent_client.upload_file(
                memory_key=run.memory_key,
                file_bytes=file_bytes,
                filename=os.path.basename(path),
                mime_type=str(asset.get("mime_type", "image/jpeg")),
            )
            upload_id = str(upload.get("id", "")).strip()
            if not upload_id:
                raise RuntimeError("Dify image upload did not return a file id")
            prepared_files.append(
                {
                    "type": "image",
                    "transfer_method": "local_file",
                    "upload_file_id": upload_id,
                }
            )
        return prepared_files

    def _update_image_analysis_summary(self, run_id: int, plan: PlannerResult, final_text: str) -> None:
        image_assets = self.store.get_image_assets_for_run(run_id)
        if not image_assets:
            return
        summary = self._build_image_analysis_summary(plan, final_text)
        for row in image_assets:
            self.store.update_image_asset_status(
                int(row["id"]),
                status="analyzed",
                analysis_summary=summary,
            )

    def _maybe_attach_recent_image_context(self, run: TaskRun, runtime_context: Dict[str, Any]) -> None:
        current_image_assets = runtime_context.get("image_assets") or []
        if current_image_assets:
            summary = self._extract_recent_image_summary(current_image_assets)
            if summary:
                runtime_context["recent_image_summary"] = summary
            return
        recent_image_assets = runtime_context.get("recent_image_assets") or []
        if not recent_image_assets:
            return
        if not self._looks_like_image_followup(run.user_goal):
            return
        selected_asset = dict(recent_image_assets[0])
        summary = self._extract_recent_image_summary([selected_asset])
        if summary:
            runtime_context["recent_image_summary"] = summary
            self.logger.info(
                format_log_event(
                    "recent_image_summary_attached",
                    run_id=run.id,
                    memory_key=run.memory_key,
                    image_asset_id=selected_asset.get("id", ""),
                )
            )
        if self._needs_original_image_review(run.user_goal, summary):
            runtime_context["image_assets"] = [selected_asset]
            self.logger.info(
                format_log_event(
                    "recent_image_context_attached",
                    run_id=run.id,
                    memory_key=run.memory_key,
                    image_asset_id=selected_asset.get("id", ""),
                )
            )

    @staticmethod
    def _looks_like_image_followup(text: str) -> bool:
        normalized = "".join(text.strip().lower().split())
        if not normalized:
            return False
        image_followup_tokens = (
            "這是什麼",
            "這個是什麼",
            "這張是什麼",
            "這張圖",
            "這張照片",
            "這個",
            "這張",
            "幫我看",
            "你覺得這是什麼",
            "看一下這個",
            "看一下這張",
            "圖裡",
            "照片裡",
            "圖片裡",
            "這是啥",
            "這看起來",
            "上面有",
            "幫我判斷",
            "幫我辨識",
        )
        return any(token in normalized for token in image_followup_tokens)

    @staticmethod
    def _extract_recent_image_summary(image_assets: List[Dict[str, Any]]) -> Dict[str, Any]:
        for asset in image_assets:
            summary = asset.get("analysis_summary")
            if isinstance(summary, dict) and summary:
                return summary
        return {}

    @staticmethod
    def _needs_original_image_review(user_goal: str, analysis_summary: Dict[str, Any]) -> bool:
        normalized = "".join(user_goal.strip().lower().split())
        if not normalized:
            return False
        detail_tokens = (
            "文字",
            "字",
            "英文",
            "內容",
            "細節",
            "標示",
            "哪家",
            "品牌",
            "型號",
            "價格",
            "店名",
            "地址",
            "電話",
            "成分",
            "日期",
            "左上角",
            "右上角",
            "下面",
            "上面",
            "讀",
            "辨識",
            "看清楚",
        )
        if not any(token in normalized for token in detail_tokens):
            return False
        visible_text = analysis_summary.get("visible_text") if isinstance(analysis_summary, dict) else []
        return not bool(visible_text)

    def _build_image_analysis_summary(self, plan: PlannerResult, final_text: str) -> Dict[str, Any]:
        summary_text = (plan.final_reply or plan.draft_user_reply or final_text or "").strip()[:2000]
        return {
            "task_type": plan.task_type,
            "goal_summary": plan.goal_summary,
            "summary": summary_text,
            "visible_objects": self._extract_visible_objects(summary_text),
            "visible_text": self._extract_visible_text(summary_text),
            "scene_or_context": self._extract_scene_or_context(summary_text, plan.goal_summary),
            "suggested_followups": self._build_image_followup_suggestions(summary_text),
            "final_reply": final_text[:2000],
        }

    @staticmethod
    def _extract_visible_objects(summary_text: str) -> List[str]:
        candidates = []
        for token in (
            "人",
            "人物",
            "餐桌",
            "桌面",
            "杯子",
            "碗",
            "飲料",
            "食物",
            "手機",
            "文件",
            "收據",
            "螢幕",
            "店面",
            "包裝",
            "招牌",
        ):
            if token in summary_text and token not in candidates:
                candidates.append(token)
        return candidates[:8]

    @staticmethod
    def _extract_visible_text(summary_text: str) -> List[str]:
        if "可辨識文字" in summary_text:
            trailing = summary_text.split("可辨識文字", 1)[1]
            parts = [part.strip(" ：:，,。") for part in trailing.replace("\n", " ").split("、")]
            return [part for part in parts if part][:8]
        return []

    @staticmethod
    def _extract_scene_or_context(summary_text: str, goal_summary: str) -> str:
        for marker in ("看起來是", "像是", "場景是", "內容是"):
            if marker in summary_text:
                return summary_text.split(marker, 1)[1].split("。", 1)[0].strip()
        return goal_summary.strip()

    @staticmethod
    def _build_image_followup_suggestions(summary_text: str) -> List[str]:
        suggestions = [
            "幫我辨識圖片中的重點物件",
            "幫我讀出圖片裡的文字",
            "幫我整理這張圖的重點",
        ]
        if any(token in summary_text for token in ("食物", "飲料", "餐桌", "菜單")):
            suggestions.append("幫我判斷這可能是什麼餐點或飲品")
        if any(token in summary_text for token in ("文件", "收據", "文字")):
            suggestions.append("幫我把圖片內容整理成條列重點")
        return suggestions[:5]

    def _maybe_cleanup_expired_image_assets(self) -> None:
        now = time.time()
        if now - self._last_image_cleanup_at < 300:
            return
        self._cleanup_expired_image_assets()
        self._last_image_cleanup_at = now

    def _cleanup_expired_image_assets(self) -> None:
        for row in self.store.list_expired_image_assets():
            path = str(row["path"] or "")
            if path and os.path.exists(path):
                os.remove(path)
                self.store.update_image_asset_status(
                    int(row["id"]),
                    status="deleted",
                    deleted_at=datetime.utcnow().isoformat(),
                )
            else:
                self.store.mark_image_asset_missing(int(row["id"]))

    @staticmethod
    def _needs_google_query_contract_retry(user_goal: str, plan: PlannerResult) -> bool:
        if plan.calendar_action or plan.task_action:
            return False
        normalized = "".join(user_goal.strip().lower().split())
        query_tokens = ("有什麼", "有哪些", "查", "看", "列出", "近期", "最近", "今天", "明天", "後天")
        asks_query = any(token in normalized for token in query_tokens)
        asks_calendar = any(token in normalized for token in ("行程", "日程", "日曆", "calendar"))
        asks_tasks = any(token in normalized for token in ("提醒", "待辦", "待办", "task", "任務", "事情"))
        return asks_query and (asks_calendar or asks_tasks)

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
        selected_option = self._match_pending_approval_option(pending, inbound.text)
        return bool(selected_option)

    def _decide_pending_followup(self, pending: Any, inbound: InboundMessage, quoted_text: str) -> str:
        if self._looks_like_casual_message(inbound.text):
            self._respond_immediate(inbound, "你好，我在。你可以直接告訴我現在要我幫你做什麼。")
            return "casual_reply"

        prompt_message_id = pending["prompt_message_id"]
        if prompt_message_id and inbound.quoted_message_id == prompt_message_id:
            return "continue_task"

        lower = inbound.text.strip().lower()
        if lower in APPROVAL_KEYWORDS or lower in REJECTION_KEYWORDS:
            return "continue_task"

        selected_option = self._match_pending_approval_option(pending, inbound.text)
        if selected_option:
            return "continue_task"

        pending_context = self.store.build_runtime_context(int(pending["run_id"]), inbound.memory_key)
        pending_context["pending_approval"] = {
            "approval_type": pending["approval_type"],
            "prompt_text": pending["prompt_text"],
            "options": json.loads(pending["options_json"] or "[]"),
        }
        pending_context["quoted_message_text"] = quoted_text
        pending_context["latest_user_message"] = inbound.text
        current_local = datetime.now().astimezone()
        pending_context["current_datetime_local"] = current_local.isoformat()
        pending_context["current_date_local"] = current_local.date().isoformat()
        pending_context["current_timezone"] = str(current_local.tzinfo or "UTC")

        try:
            plan = self.agent_client.plan(
                memory_key=inbound.memory_key,
                user_goal=inbound.text,
                runtime_context=pending_context,
            )
        except Exception as err:
            self.logger.exception(
                format_log_event(
                    "pending_followup_decision_failed",
                    run_id=pending["run_id"],
                    memory_key=inbound.memory_key,
                    error_type=type(err).__name__,
                )
            )
            return "new_task"

        self.logger.info(
            format_log_event(
                "pending_followup_decided",
                run_id=pending["run_id"],
                memory_key=inbound.memory_key,
                conversation_mode=plan.conversation_mode,
                context_usage=plan.context_usage,
                task_type=plan.task_type,
            )
        )
        if plan.conversation_mode == "continue_task":
            return "continue_task"
        if plan.conversation_mode == "casual_reply":
            reply = plan.final_reply or "你好，我在。你可以直接告訴我現在要我幫你做什麼。"
            self._respond_immediate(inbound, reply)
            return "casual_reply"
        return "new_task"

    @staticmethod
    def _looks_like_casual_message(text: str) -> bool:
        normalized = "".join(text.strip().lower().split())
        return normalized in {
            "hi",
            "hello",
            "hey",
            "嗨",
            "哈囉",
            "你好",
            "早安",
            "午安",
            "晚安",
            "哈哈",
            "謝謝",
            "thanks",
            "thankyou",
        }

    def _looks_like_google_auth_decline(self, text: str) -> bool:
        normalized = "".join(text.strip().lower().split())
        decline_tokens = ("不用", "不要", "先不要", "不需要", "只要文字", "文字稿", "純文字", "不用進入行事曆", "不要行事曆")
        return any(token in normalized for token in decline_tokens)

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
        calendar_action, task_action = self._normalized_google_workspace_actions(plan)
        action = calendar_action or task_action
        if not action:
            return {}
        if not self._should_allow_google_workspace_action(run.user_goal, plan):
            self.logger.info(
                format_log_event(
                    "google_workspace_action_skipped",
                    run_id=run.id,
                    memory_key=run.memory_key,
                    task_type=plan.task_type,
                )
            )
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
            if calendar_action:
                result = self._execute_calendar_action(run, token_payload, calendar_action)
                if result.get("status") == "awaiting_approval":
                    return result
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
            if task_action:
                result = self._execute_task_action(run, token_payload, task_action)
                if result.get("status") == "awaiting_approval":
                    return result
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

    @staticmethod
    def _normalize_google_workspace_action(action: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(action, dict):
            return {}
        operation = str(action.get("operation", "")).strip()
        if not operation:
            return {}
        normalized = dict(action)
        normalized["operation"] = operation
        return normalized

    def _normalized_google_workspace_actions(self, plan: PlannerResult) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        return (
            self._normalize_google_workspace_action(plan.calendar_action),
            self._normalize_google_workspace_action(plan.task_action),
        )

    def _should_allow_google_workspace_action(self, user_goal: str, plan: PlannerResult) -> bool:
        calendar_action, task_action = self._normalized_google_workspace_actions(plan)
        operation = str((calendar_action or task_action).get("operation", "")).strip()
        if not operation:
            return False
        if operation in {"list_events", "list_tasks", "update_event", "cancel_event", "update_task", "complete_task", "delete_task"}:
            return True
        normalized = "".join(user_goal.strip().lower().split())
        explicit_google_schedule_tokens = (
            "googlecalendar",
            "google行事曆",
            "google日曆",
            "googletasks",
            "提醒我",
            "加入行事曆",
            "加到行事曆",
            "建立提醒",
            "建立行事曆",
            "排進行事曆",
            "放進行事曆",
            "calendar",
            "tasks",
        )
        has_explicit_google_schedule = any(token in normalized for token in explicit_google_schedule_tokens)
        if plan.task_type == "trip_planning":
            return has_explicit_google_schedule
        return True

    def _execute_calendar_action(self, run: TaskRun, token_payload: Dict[str, Any], action: Dict[str, Any]) -> Dict[str, Any]:
        operation = str(action.get("operation", "")).strip()
        event_id, pending_result = self._resolve_service_target(run, "google_event", action)
        if pending_result:
            return pending_result
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

    def _execute_task_action(self, run: TaskRun, token_payload: Dict[str, Any], action: Dict[str, Any]) -> Dict[str, Any]:
        operation = str(action.get("operation", "")).strip()
        task_id, pending_result = self._resolve_service_target(run, "google_task", action)
        if pending_result:
            return pending_result
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

    def _resolve_service_target(
        self,
        run: TaskRun,
        kind: str,
        action: Dict[str, Any],
    ) -> Tuple[str, Optional[Dict[str, Any]]]:
        explicit_id = str(action.get("event_id") or action.get("task_id") or "").strip()
        if explicit_id:
            return explicit_id, None

        selected_id = self._resolve_selected_service_id(kind)
        if selected_id:
            return selected_id, None

        operation = str(action.get("operation", "")).strip()
        if operation in {"create_event", "create_task", "list_events", "list_tasks"}:
            return "", None

        candidates = self._find_service_candidates(kind, action)
        if len(candidates) == 1:
            return str(candidates[0]["service_id"]), None
        if not candidates:
            return "", None
        return "", self._request_service_target_selection(run, kind, candidates)

    def _resolve_selected_service_id(self, kind: str) -> str:
        recent = self.store.get_recent_memory_artifacts(
            self.current_memory_key_for_resolution,
            kinds=["approval_response"],
            limit=5,
        )
        for row in recent:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except json.JSONDecodeError:
                metadata = {}
            selected = metadata.get("selected_option") or {}
            if str(selected.get("kind", "")).strip() != kind:
                continue
            service_id = str(selected.get("service_id") or "").strip()
            if service_id:
                return service_id
        return ""

    def _find_service_candidates(self, kind: str, action: Dict[str, Any]) -> List[Dict[str, Any]]:
        target_name = self._normalize_service_target_name(
            str(action.get("summary") or action.get("title") or "").strip()
        )
        recent = self.store.get_recent_memory_artifacts(self.current_memory_key_for_resolution, kinds=[kind], limit=8)
        candidates: List[Dict[str, Any]] = []
        partial_matches: List[Dict[str, Any]] = []
        for row in recent:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except json.JSONDecodeError:
                metadata = {}
            candidate_name = str(metadata.get("summary") or metadata.get("title") or row["content"] or "").strip()
            candidate_norm = self._normalize_service_target_name(candidate_name)
            service_id = str(metadata.get("event_id") or metadata.get("task_id") or row["ref_key"] or "").strip()
            if not service_id:
                continue
            candidate = {
                "kind": kind,
                "service_id": service_id,
                "title": candidate_name or service_id,
                "scheduled_at": str(metadata.get("start") or metadata.get("due") or ""),
                "status": str(metadata.get("status") or ""),
                "link": str(metadata.get("html_link") or metadata.get("web_view_link") or ""),
            }
            if not target_name:
                candidates.append(candidate)
                continue
            if candidate_norm == target_name:
                candidates.append(candidate)
            elif target_name in candidate_norm or candidate_norm in target_name:
                partial_matches.append(candidate)
        if candidates:
            return candidates
        if len(partial_matches) == 1:
            return partial_matches
        if partial_matches:
            return partial_matches
        return candidates

    def _request_service_target_selection(
        self,
        run: TaskRun,
        kind: str,
        candidates: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        lines = ["我找到多個可能要修改的事項，請直接回覆選項編號或完整事項名稱："]
        options: List[Dict[str, Any]] = []
        for idx, candidate in enumerate(candidates, start=1):
            label = self._format_service_candidate_label(candidate)
            lines.append(f"{idx}. {label}")
            options.append(
                {
                    "kind": kind,
                    "service_id": candidate["service_id"],
                    "title": candidate["title"],
                    "label": label,
                    "scheduled_at": candidate.get("scheduled_at", ""),
                    "status": candidate.get("status", ""),
                    "link": candidate.get("link", ""),
                }
            )
        prompt = "\n".join(lines)
        approval_id = self.store.create_pending_approval(
            run_id=run.id,
            memory_key=run.memory_key,
            approval_type="service_target_selection",
            prompt_text=prompt,
            options=options,
        )
        self.store.update_run_status(
            run.id,
            status="waiting_approval",
            requires_approval=True,
            current_phase="waiting_target_selection",
        )
        push_target = self._memory_key_to_push_target(run.memory_key)
        message_ids = self.messenger.push_text(push_target, prompt)
        self.store.store_bot_messages(message_ids, self.messenger.split_for_storage(prompt))
        if message_ids:
            self.store.set_approval_prompt_message(approval_id, message_ids[0])
        self.store.append_history(run.memory_key, "assistant", prompt)
        self.logger.info(
            format_log_event(
                "service_target_selection_requested",
                run_id=run.id,
                approval_id=approval_id,
                memory_key=run.memory_key,
                service_kind=kind,
                candidate_count=len(candidates),
            )
        )
        return {"status": "awaiting_approval"}

    def _format_service_candidate_label(self, candidate: Dict[str, Any]) -> str:
        title = str(candidate.get("title", "")).strip() or "未命名事項"
        scheduled_at = str(candidate.get("scheduled_at", "")).strip()
        if scheduled_at:
            readable = scheduled_at.replace("T", " ")
            readable = readable.replace("+08:00", "").replace("Z", "")
            return f"{title}（{readable}）"
        return title

    def _normalize_service_target_name(self, value: str) -> str:
        return "".join(str(value).strip().lower().split())

    def _approval_requires_option_selection(self, pending: Any) -> bool:
        return str(pending["approval_type"]).strip() == "service_target_selection"

    def _match_pending_approval_option(self, pending: Any, text: str) -> Optional[Dict[str, Any]]:
        try:
            options = json.loads(pending["options_json"] or "[]")
        except json.JSONDecodeError:
            return None
        if not options:
            return None
        response = str(text).strip()
        if response.isdigit():
            index = int(response) - 1
            if 0 <= index < len(options):
                return dict(options[index])
        normalized = self._normalize_service_target_name(response)
        matches = []
        for option in options:
            title = self._normalize_service_target_name(str(option.get("title", "")))
            label = self._normalize_service_target_name(str(option.get("label", "")))
            if normalized and normalized in {title, label}:
                matches.append(option)
            elif normalized and (normalized in title or normalized in label):
                matches.append(option)
        if len(matches) == 1:
            return dict(matches[0])
        return None

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
        browser_request = self._normalize_browser_request(plan.browser_request)
        if not browser_request:
            return {}
        self.logger.info(
            format_log_event(
                "browser_request_deferred",
                run_id=run.id,
                memory_key=run.memory_key,
                domain=browser_request.get("domain", ""),
                intent=browser_request.get("intent", ""),
            )
        )
        return {
            "final_reply": "網站自動操作到付款前的功能會放到下一階段，目前先提供個人記憶、行事曆與提醒事項服務。",
        }

    @staticmethod
    def _normalize_browser_request(browser_request: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(browser_request, dict):
            return {}
        domain = str(browser_request.get("domain", "")).strip()
        intent = str(browser_request.get("intent", "")).strip()
        if not domain or not intent:
            return {}
        normalized = dict(browser_request)
        normalized["domain"] = domain
        normalized["intent"] = intent
        return normalized

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
            "conversation_mode": plan.conversation_mode,
            "context_usage": plan.context_usage,
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
            "requested_outputs": plan.requested_outputs,
            "document_title": plan.document_title,
        }
